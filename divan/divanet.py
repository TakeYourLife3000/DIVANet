import torch, os, random, warnings
import torch.nn as nn
import torch.optim as optim
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, TensorDataset, DistributedSampler
from torch.optim.swa_utils import AveragedModel, SWALR

from psutil import cpu_count
from tqdm.auto import tqdm
import logging, yaml, time, inspect
import numpy as np

from divan.check import *
from divan.utils import *
from divan.module import *
from divan.parse_task import *

warnings.simplefilter("ignore")
state_PATH = 'HGNetv2.pt'

#dataset = Dataset_Manager('dataset', batch_size=64, channels=[None, 'RG'], cut_channels=[True, False], fix_mean=[True, True], RAM=True, shuffle=True)

class model_Manager(nn.Module):
    def __init__(self, model_setting,
                 channels='RGB',
                 amp=True,
                 device='cuda',
                 seed=123
                 ):
        super().__init__()
        apply_config(self, __file__)
        apply_args(self)
        self.model_weight = False
        self.glob_amp = amp

        self.set_seed(seed)
        if model_setting.endswith('.pt'):
            self._load_state(model_setting)
            #train/{self.model_setting.split('.')[0]}/weight/best.pt
        else:
            self._read_model()
    def to(self, device):
        assert hasattr(self, 'model')
        self.device, self.device_use = self._ready_device(device)

        self.model = self.model.to(self.device)
        self.ema = self.ema.to(self.device)

    def forward(self, x):
        pass
        with torch.autocast(device_type=self.device):
            return self.model(x.float().to(self.device))

    def fit(self, dataset_path, epochs,
            label_path=["train.txt", "val.txt", "test.txt"],
            size=224,
            batch_size=32,
            AMP=True,
            EMA=True,
            pin_memory=False,
            shuffle=True,
            silence=False,
            fix_mean=False,
            cut_channels=False,
            random_p=0.8,
            num_workers=-1,
            RAM='auto',
            cutmix_p=1,
            ncols=90,
            RAM_lim=0.925,
            ):
        apply_args(self)
        self._build_dataset()
        self.ema_start = 0 if EMA is True else -1 if not EMA else EMA
        dataset_class_num = self.Dataset.class_num
        if hasattr(self, 'class_num'):
            if dataset_class_num != self.class_num:
                self.class_num = dataset_class_num
                self._fclayer_resize(self.class_num)
        else:
            self.class_num = dataset_class_num
            self._fclayer_resize(self.class_num)

        self.to(self.device)
        self.epoch, self.epochs = self.epoch if hasattr(self, "epoch") else [0, epochs]

        for epoch in range(self.epoch, self.epochs):
            pass

    def valid(self):
        assert hasattr(self, "state_dict")

    def _read_model(self):
        raise NotImplementedError

    def _fclayer_resize(self, num_class):
        self.model = fclayer_resize(self.model, num_class)
        self.ema = fclayer_resize(self.ema, num_class)

    def _save_state(self, epoch, best=False):
        state_dict = {'model_setting': self.model_setting,
                      'num_class': self.num_class,
                      'epoch': [epoch, self.epochs],
                      'model_dict': self.model.state_dict(),
                      'ema_dict': self.ema.state_dict(),
                      'optimizer_dict': self.optimizer.state_dict(),
                      'scheduler_dict': self.scheduler.state_dict(),
                      'ema_scheduler_dict': self.swa_scheduler.state_dict()
                      }
        if best:
            getattr(self, "state_dict", state_dict)

        torch.save(state_dict, self.state_PATH)

    def _load_state(self, state_PATH=None):
        assert state_PATH.endswith('.pt')
        state_PATH = self.state_PATH if state_PATH is None else state_PATH
        self.state_dict = torch.load(state_PATH)
        apply_kwargs(self, self.state_dict)
        self._read_model()
        self._load_model()

    def _load_model(self):
        self.model.load_state_dict(self.model_dict)
        self.ema.load_state_dict(self.ema_dict)
        self.optimizer.load_state_dict(self.optimizer_dict)
        self.scheduler.load_state_dict(self.scheduler_dict)
        self.swa_scheduler.load_state_dict(self.ema_scheduler_dict)
        self.model_weight = True

    def _ready_device(self, device=None):
        device = self.device.split(':') if device is None else device.split(':')
        device, cuda_idx = device if len(device) > 1 else [device[0], None]
        device = device if torch.cuda.is_available() and device != 'cpu' else 'cpu'
        cuda_idx = f':{cuda_idx}' if cuda_idx is not None and device != 'cpu' else cuda_idx
        return chose_cuda(device) if cuda_idx is None else f'{device}{cuda_idx}', device

    def _build_dataset(self):
        self.Dataset = Dataset_Manager(dataset_path=self.dataset_path,
                                       label_path=self.label_path,
                                       channels=self.channels,
                                       size=self.size,
                                       batch_size=self.batch_size,
                                       pin_memory=self.pin_memory,
                                       shuffle=self.shuffle,
                                       silence=self.silence,
                                       fix_mean=self.fix_mean,
                                       cut_channels=self.cut_channels,
                                       random_p=self.random_p,
                                       num_workers=self.num_workers,
                                       RAM=self.RAM,
                                       cutmix_p=self.cutmix_p,
                                       ncols=self.ncols,
                                       RAM_lim=self.RAM_lim
                                       )

    def _close_cutmix(self):
        self.Dataset.close_cutmix()

    def _run_loader(self, dataloader, epoch):
        _training = self.model.training
        _desc = self._train_string if _training else self._eval_string
        correct, total = 0, 0
        tol_loss = 0
        pbar = tqdm(dataloader, desc=self.table_with_fix(_desc(epoch)), ncols=110)
        for _idx, (img, label) in enumerate(pbar):
            self.optimizer.zero_grad()
            img = img.float().to(self.device, non_blocking=dataloader.pin_memory)
            label = label.to(self.device, non_blocking=dataloader.pin_memory)
            epoch = torch.inf if isinstance(epoch, str) else epoch
            _epoch = 'eval' if epoch == torch.inf else epoch

            with torch.autocast(device_type=self.device_use, enabled=self.AMP, dtype=torch.float16):
                with torch.set_grad_enabled(_training):
                    module = self.ema if not _training and self.ema and epoch >= self.ema_start else self.model
                    out = module(img)
                    loss = self.loss_function(out, label)

            tol_loss += loss.item()
            if _training:
                pbar.set_description(self.table_with_fix(_desc(_epoch, tol_loss / (_idx + 1))))
                #if self.amp and self.scaler != None:
                #    self.scaler.scale(loss).backward()
                #    self.scaler.step(self.optimizer)
                #    self.scaler.update()
                #else:
                #loss.backward()
                #self.optimizer.step()




    def _training_step(self, epoch):
        torch.set_grad_enabled(True)
        logging.warning(f'\n{self.table_with_fix(self.fit_training_col)}')
        self.model.train()
        self._run_loader(self.Dataset.train_loader, epoch)

        logging.warning(f'{self.table_with_fix(self.fit_eval_col)}')
        self.model.eval()
        self._run_loader(self.Dataset.val_loader, epoch)

    def _testing_step(self):
        logging.warning(f'{self.table_with_fix(self.fit_training_col)}')
        self.model.train()
        self._run_loader(self.Dataset.train_loader, 'val')

        logging.warning(f'{self.table_with_fix(self.fit_eval_col)}')
        self.model.eval()
        self._run_loader(self.Dataset.test_loader, 'val')

    def _train_string(self, epoch, loss=np.inf):
        return (f"{epoch}/{self.epochs}",
                f"{round(torch.cuda.memory_allocated(device = self.device)/1000000000, 2) if self.device_use == 'cuda' else 0.0}G",
                f"{round(loss, 3) if loss != np.inf else loss}",
                f"{self.size}"
                )

    def _eval_string(self, loss=np.inf, acc=0):
        return (f" ",
                f" ",
                f"{round(loss, 3) if loss != np.inf else loss}",
                f"{round(acc, 3)}")

    def loader_perf(self, loader):
        start = time.monotonic()
        for data in tqdm(loader, desc=f"|{self.block_name} - speed test|"):
            _ = data
        logging.debug(f"{self.block_name}: loader loop time: {round(time.monotonic()-start, 2)}s")

    @staticmethod
    def table_with_fix(col, fix_len=13):
        formatted_str = '|'.join(f'{x:^{fix_len}}' for x in col)
        return f'|{formatted_str}|'

    @staticmethod
    def set_seed(seed):
        if isinstance(seed, int):
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)

class DIVAN(model_Manager):
    def _read_model(self):
        self.label_smoothing = 0.1
        self.lr = 0.0005
        self.weight_decay = 0
        self.epochs = 30
        self.state_PATH = self.save_PATH + f"train/{self.model_setting.split('.')[0]}/weight/best.pt"

        self.yaml = self._read_yaml(self.model_setting.split('.yaml')[0])
        self.yaml["nc"] = self.num_class if hasattr(self, "num_class") else self.yaml["nc"]
        intput_channels = len(self.channels) if self.channels != 'auto' else self.channels

        self.model = Divanet_model(self.yaml, intput_channels)

        self.ema = AveragedModel(self.model)

        self.to(device=self.device)

        self.loss_function = nn.CrossEntropyLoss(label_smoothing=self.label_smoothing)
        self.optimizer = torch.optim.Adam(self.model.parameters(),
                                           lr=self.lr,
                                           weight_decay=self.weight_decay,
                                           )

        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.optimizer, T_max=self.epochs, eta_min=self.lr/25, last_epoch=-1)
        self.swa_scheduler = SWALR(self.optimizer, swa_lr=self.lr*1.5, anneal_strategy="cos")

        try:
            self.scaler = getattr(torch, self.device_use).amp.GradScaler(enabled=self.amp)
        except:
            self.scaler = None

    def _read_yaml(self, yaml_path):
        with open(f'{self.cfg_PATH}{yaml_path}.yaml', 'r', encoding="utf-8") as stream:
            return yaml.safe_load(stream)


if __name__ == "__main__":
    pass
    #model_Manager()
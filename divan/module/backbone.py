import time

import torchvision, logging
import torch.nn as nn
import torch, timm

import contextlib, inspect, yaml, ast, re

from divan.module.kan_convs import *
from divan.module.block import *
from divan.module.conv import *
from divan.module.utils import *
from divan.module.attention import *
from divan.module.head import *

block_name = 'backbone'

__all__ = ["torch_model", "timm_model", "yaml_model"]

def torch_model(model, usage_args=False, num_classes=50):
    weights = usage_args if usage_args is not False else None
    if type(model) == str:
        try:
            model = getattr(torchvision.models, model)

        except:
            logging.warning(f'{block_name}: model not exist')
            return None

    if not isinstance(usage_args, int):

        try:
            model = model(weights=weights)

        except:
            model = model(weights='DEFAULT')
            weights= 'DEFAULT'
            logging.warning(f'{block_name}: weights not exist')

        finally:
            logging.info(f'{block_name}: Loading model - {model.__name__}')
            logging.info(f'{block_name}: Loading weights - {weights}')

    else:
        model = inlayer_resize(model(False), usage_args)
    return model

def timm_model(model, usage_args=False, num_classes=50):
    weights = usage_args if usage_args is not False else None
    if type(model) == str:
        try:
            model = timm.create_model(model, pretrained=weights, num_classes=num_classes)

        except:
            try:
                model = timm.create_model(model, pretrained=False, num_classes=num_classes)
                weights = False
                logging.warning(f'{block_name}: weights not exist')
            except:
                logging.warning(f'{block_name}: model not exist')
                return None
        finally:
            logging.info(f'{block_name}: Loading model - {model.__class__.__name__}')
            logging.info(f'{block_name}: Loading weights - {weights}')
    else:
        model = timm.create_model(model, in_chans=usage_args, num_classes=num_classes)
    return model


class yaml_model(nn.Module):
    def __init__(self, _dict, _input_channels, device):
        super().__init__()
        self.sequential, self.save_idx, self.fc_resize = self.parse_model(_dict, _input_channels)
        self.device = device

    def forward(self, x):
        out = {-1: x}
        for module in self.sequential:
            save_i = [-1] + [i for i in module.f if i in self.save_idx]
            out.update(out.fromkeys(save_i, module([out[k].to(self.device) for k in module.f] if len(module.f) > 1 else out[module.f[0]].to(self.device))))
        return out[-1]

    @staticmethod
    def parse_model(_dict, _input_channels, input_pool='Pool_Conv'): # model_dict, input_channels(3)

        max_channels = float("inf")
        fc_resize = True
        num_class, act, scales, c1_pool = (_dict.get(x) for x in ("nc", "activation", "scales","c1_pool"))

        if scales:
            scale = _dict.get("scale")
            if not scale:
                scale = tuple(scales.keys())[0]
            logging.info(f"{block_name}: Assuming scale='{scale}'")
                # LOGGER.warning(f"WARNING ⚠️ no model scale passed. Assuming scale='{scale}'.")
            depth, width, max_channels = scales[scale]

        if act:
            Conv.default_act = Activations(act)(inplace=True)
            #print(f"{('activation:')} {Conv.default_act}")  # print

        #if verbose:
            #LOGGER.info(f"\n{'':>3}{'from':>20}{'n':>3}{'params':>10}  {'module':<45}{'arguments':<30}")

        ch = [_input_channels]
        layers, save, c2 = [], [], ch[-1]  # layers, savelist, ch out

        if _input_channels == 'auto':
            ch = [1]
            input_pool = globals()[input_pool]
            m_seq = input_pool(*[16, 3, Conv, act, ['Avg', 'Max']])

            t = str(input_pool)[8:-2].replace("__main__.", "")
            input_pool.np = sum(x.numel() for x in m_seq.parameters())  # number params
            m_seq.i, m_seq.f, m_seq.type = -1, [-1], t
            layers.append(m_seq)

        full_dict = _dict["backbone"]
        if _dict.get("head"):
            full_dict += _dict["head"]
        else:
            print('no head')

        for i, (f, n, m, args) in enumerate(full_dict):  # from, number, module, args
            assert max([f] if isinstance(f, int) else f) < i
            m = m.split('_') if m not in ['torch_model', 'timm_model'] else [m]
            if len(m) == 2:
                if 'KA' in m[1] and 'NConv' in m[1]:
                    m, _conv = m[0] + '_KAN', m[1]
                elif m[0] + m[1] in globals():
                    m, _conv = m[0] + m[1], None
                else:
                    m, _conv = m[0], m[1]

            elif len(m) == 1:
                m, _conv = m[0], None
            else:
                raise ValueError(f'invalid model: {m}')

            m = getattr(nn, m[3:]) if m.startswith('nn.') else globals()[m]
            _conv = globals()[_conv] if _conv else Conv

            for j, a in enumerate(args):
                if isinstance(a, str):
                    with contextlib.suppress(ValueError):
                        args[j] = locals()[a] if a in locals() else ast.literal_eval(a)

            if m not in [nn.BatchNorm2d, Concat, torch_model, timm_model,
                         ChannelAttention, SpatialAttention, CBAM
                         ]:
                c1, c2 = ch[f], args[0]

                if c2 != 'nc':  # if c2 not equal to number of classes (i.e. for Classify() output)
                    c2 = make_divisible(min(c2, max_channels) * width, 8)

                else:
                    c2 = num_class

                args = [c1, c2, *args[1:]]
                if m in {C1, C2, C2f, C3, C2f_KAN, C3_KAN}:
                    _arg = inspect.getfullargspec(m)[0]
                    args.insert(_arg.index('n') - 1 if 'self' in _arg else 0, n)  # number of repeats
                    args.insert(_arg.index('_conv') - 1 if 'self' in _arg else 0, _conv)
                    n = 1

            elif m in [ChannelAttention, SpatialAttention, CBAM]:
                c2 = ch[f]
                args = [c1, *args]

            elif m is nn.BatchNorm2d:
                args = [ch[f]]

            elif m is Concat:
                c2 = sum(ch[x] for x in f)

            elif m in [torch_model, timm_model]:
                if _input_channels == "auto":
                    _arg = inspect.getfullargspec(m)[0]
                    args.insert(_arg.index("usage_args"), 1)
                    args.insert(_arg.index("num_classes"), num_class)
                if m == timm_model:
                    fc_resize = False
                c2 = ch[f]

            else:
                c2 = ch[f]

            m_seq = nn.Sequential(*(m(*args) for _ in range(n))) if n > 1 else m(*args)  # module
            t = str(m)[8:-2].replace("__main__.", "")
            m.np = sum(x.numel() for x in m_seq.parameters())  # number params
            m_seq.i, m_seq.f, m_seq.type = i, ([f] if isinstance(f, int) else f), t  # attach index, 'from' index, type
            #if verbose:
            #    LOGGER.info(f"{i:>3}{str(f):>20}{n_:>3}{m.np:10.0f}  {t:<45}{str(args):<30}")  # print

            save.extend(x for x in ([f] if isinstance(f, int) else f) if x != -1 and x < i)  # append to savelist
            layers.append(m_seq)
            if i == 0:
                ch = []
            ch.append(c2)
        return nn.Sequential(*layers), sorted(save), fc_resize

if __name__ == '__main__':
    FORMAT = '[%(levelname)s] | %(asctime)s | %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=FORMAT, datefmt='%Y-%m-%d %H:%M')
    print(torch_model('resnet34'))



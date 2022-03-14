import torch
import torchvision
import torchvision.models

import gc
import io
import inspect
import logging
import os
import sys
import unittest

import numpy as np

from tinynn.converter import TFLiteConverter
from tinynn.graph.tracer import model_tracer, trace
from tinynn.graph.quantization.quantizer import QATQuantizer
from common_utils import IS_CI, collect_custom_models, collect_torchvision_models, prepare_inputs


HAS_TF = False
try:
    import tensorflow as tf

    HAS_TF = True
except ImportError:
    pass


def data_to_tf(inputs, input_transpose):
    tf_inputs = list(map(lambda x: x.cpu().detach().numpy(), inputs))
    for i in range(len(tf_inputs)):
        if input_transpose[i]:
            tf_inputs[i] = np.transpose(tf_inputs[i], [0, 2, 3, 1])
    return tf_inputs


def get_tflite_out(model_path, inputs, skip_run=False):
    interpreter = tf.lite.Interpreter(model_path=model_path)
    interpreter.allocate_tensors()

    # Get input and output tensors.
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    for i in range(len(inputs)):
        interpreter.set_tensor(input_details[i]['index'], inputs[i])

    if not skip_run:
        interpreter.invoke()

    outputs = []
    for i in range(len(output_details)):
        output_data = interpreter.get_tensor(output_details[i]['index'])
        outputs.append(output_data)

    return outputs


# Unsupported models
# resnext: group convs
# regnet: group convs
# yolov4: 5-d slices
BLACKLIST = (
    'resnext50_32x4d',
    'resnext101_32x8d',
    'Build_Model',
    'regnet_x_16gf',
    'regnet_x_1_6gf',
    'regnet_x_32gf',
    'regnet_x_3_2gf',
    'regnet_x_400mf',
    'regnet_x_800mf',
    'regnet_x_8gf',
    'regnet_y_16gf',
    'regnet_y_1_6gf',
    'regnet_y_32gf',
    'regnet_y_3_2gf',
    'regnet_y_400mf',
    'regnet_y_800mf',
    'regnet_y_8gf',
    'regnet_y_128gf',
)

# Those extremely-large models will be skipped to save memory and time
RUN_BLACKLIST = (
    'vit_b_16',
    'vit_b_32',
    'vit_l_16',
    'vit_l_32',
)


class TestModelMeta(type):
    @classmethod
    def __prepare__(mcls, name, bases):
        d = dict()
        test_classes = collect_torchvision_models()
        for test_class in test_classes:
            test_name = f'test_torchvision_model_{test_class.__name__}'
            simple_test_name = test_name + '_simple'
            d[simple_test_name] = mcls.build_model_test(test_class)
        # test_classes = collect_custom_models()
        # for test_class in test_classes:
        #     test_name = f'test_custom_model_{test_class.__name__}'
        #     simple_test_name = test_name + '_simple'
        #     d[simple_test_name] = mcls.build_model_test(test_class)
        return d

    @classmethod
    def build_model_test(cls, model_class):
        def prepare_q_model(model_name):
            args = ()
            kwargs = dict()
            if model_name in ('googlenet', 'inception_v3'):
                kwargs = {'aux_logits': False}

            with model_tracer():
                m = model_class(*args, **kwargs)
                m.cpu()
                m.eval()

                inputs = prepare_inputs(m)

                config = {'remove_weights_after_load': True}
                if sys.platform == 'win32':
                    config.update({'backend': 'fbgemm', 'per_tensor': False})

                quantizer = QATQuantizer(m, inputs, work_dir='out', config=config)
                qat_model = quantizer.quantize()

            return qat_model, inputs

        def f(self):
            model_name = model_class.__name__
            model_file = model_name
            model_file += '_qat_simple'

            if model_name in BLACKLIST:
                raise unittest.SkipTest('IN BLACKLIST')

            skip_run = False
            if model_name in RUN_BLACKLIST or IS_CI:
                skip_run = True

            if os.path.exists(f'out/{model_file}.tflite'):
                raise unittest.SkipTest('TESTED')

            qat_model, inputs = prepare_q_model(model_name)

            with torch.no_grad():
                qat_model.eval()
                qat_model.cpu()

                qat_model = torch.quantization.convert(qat_model)

                out_path = f'out/{model_file}.tflite'

                extra_kwargs = {}
                if IS_CI:
                    out_pt = f'out/{model_file}.pt'
                    extra_kwargs.update({'dump_jit_model_path': out_pt, 'gc_when_reload': True})
                if sys.platform == 'win32':
                    extra_kwargs.update({'quantize_target_type': 'int8'})

                converter = TFLiteConverter(qat_model, inputs, out_path, **extra_kwargs)
                converter.convert()

                if IS_CI:
                    os.remove(out_pt)

                if HAS_TF:
                    outputs = converter.get_outputs()
                    input_transpose = converter.input_transpose
                    input_tf = data_to_tf(inputs, input_transpose)
                    tf_outputs = get_tflite_out(out_path, input_tf, skip_run)
                    self.assertTrue(len(outputs) == len(tf_outputs))

                if IS_CI and os.path.exists(out_path):
                    os.remove(out_path)

        return f


class TestModel(unittest.TestCase, metaclass=TestModelMeta):
    pass


if __name__ == '__main__':
    unittest.main()

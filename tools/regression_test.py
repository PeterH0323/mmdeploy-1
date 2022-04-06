# Copyright (c) OpenMMLab. All rights reserved.
import argparse
import logging
import os
from collections import OrderedDict
from pathlib import Path

import pandas as pd
import yaml
from torch.hub import download_url_to_file
from torch.multiprocessing import set_start_method

from mmdeploy.utils import get_root_logger, load_config, \
    get_backend, is_dynamic_shape


def parse_args():
    parser = argparse.ArgumentParser(description='Regression Test')
    parser.add_argument(
        '--deploy-yml',
        nargs='+',
        help='regression test yaml path.',
        default=['./configs/mmdet/mmdet_regression_test.yaml'])
    parser.add_argument(
        '--test-type',
        type=str,
        help='`test type',
        default='precision',
        choices=['precision', 'convert'])
    parser.add_argument(
        '--backends',
        nargs='+',
        help='test specific backend(s)',
        default=['all'])
    parser.add_argument(
        '--work-dir',
        type=str,
        help='the dir to save logs and models',
        default='../mmdeploy_regression_working_dir')
    parser.add_argument(
        '--device-id', type=str, help='`the CUDA device id', default='cuda')
    parser.add_argument(
        '--log-level',
        help='set log level',
        default='INFO',
        choices=list(logging._nameToLevel.keys()))
    args = parser.parse_args()

    return args


def get_model_metafile_info(global_info, model_info, logger):
    """Get model metafile information.

    Args:
        global_info (dict): global info from deploy yaml.
        model_info (dict):  model info from deploy yaml.
        logger (logging.Logger): logger.

    Returns:
        Dict: Meta infos of each model config
    """

    # get info from global_info and model_info
    checkpoint_dir = global_info.get('checkpoint_dir', None)
    assert checkpoint_dir is not None

    codebase_dir = global_info.get('codebase_dir', None)
    assert codebase_dir is not None

    codebase_name = global_info.get('codebase_name', None)
    assert codebase_name is not None

    model_config_files = model_info.get('model_configs', [])
    assert len(model_config_files) > 0

    # make checkpoint save directory
    checkpoint_save_dir = Path(checkpoint_dir).joinpath(
        codebase_name, model_info.get('name'))
    checkpoint_save_dir.mkdir(parents=True, exist_ok=True)

    # get model metafile info
    metafile_path = Path(codebase_dir).joinpath(model_info.get('metafile'))
    with open(metafile_path) as f:
        metafile_info = yaml.load(f, Loader=yaml.FullLoader)

    model_meta_info = dict()
    for meta_model in metafile_info.get('Models'):
        if str(meta_model.get('Name')) + '.py' not in model_config_files:
            # skip if the model not in model_config_files
            logger.warning(f'{str(meta_model.get("Name")) + ".py"} '
                           f'not in {model_config_files}, pls check ! '
                           'Skip it...')
            continue

        # get meta info
        model_meta_info.update({meta_model.get('Name'): meta_model})

        # get weight url
        weights_url = meta_model.get('Weights')
        weights_name = str(weights_url).split('/')[-1]
        weights_save_path = checkpoint_save_dir.joinpath(weights_name)
        if weights_save_path.exists() and \
                not global_info.get('checkpoint_force_download', False):
            continue

        # Download weight
        logger.info(f'Downloading {weights_url} to {weights_save_path}')
        download_url_to_file(
            weights_url, str(weights_save_path), progress=True)

        # check weather the weight download successful
        if not weights_save_path.exists():
            raise FileExistsError(f'Weight {weights_name} download fail')

    logger.info('All models had been downloaded successful !')
    return model_meta_info, checkpoint_save_dir, codebase_dir


def update_report(
        report_dict,
        model_name,
        model_config,
        model_checkpoint_name,
        dataset,
        backend_name,
        deploy_config,
        static_or_dynamic,
        conversion_result,
        fps,
        metric_info,
        test_pass,
):
    """Update report information.

    Args:
        report_dict (dict): Report info dict.
        model_name (str): Model name.
        model_config (str): Model config name.
        model_checkpoint_name (str): Model checkpoint name.
        dataset (str): Dataset name.
        backend_name (str): Backend name.
        deploy_config (str): Deploy config name.
        static_or_dynamic (str): Static or dynamic.
        conversion_result (str): Conversion result: Successful or Fail.
        fps (str): Inference speed (ms/im).
        metric_info (list): Metric info list of the ${modelName}.yml.
        test_pass (str): Test result: Pass or Fail.
    """
    report_dict.get('model_name').append(model_name)
    report_dict.get('model_config').append(model_config)
    report_dict.get('model_checkpoint_name').append(model_checkpoint_name)
    report_dict.get('dataset').append(dataset)
    report_dict.get('backend_name').append(backend_name)
    report_dict.get('deploy_config').append(deploy_config)
    report_dict.get('static_or_dynamic').append(static_or_dynamic)
    report_dict.get('conversion_result').append(conversion_result)
    report_dict.get('fps').append(fps)

    for metric in metric_info:
        for metric_name, metric_value in metric.items():
            metric_name = str(metric_name)
            report_dict.get(metric_name).append(metric_value)

    report_dict.get('test_pass').append(test_pass)


def get_pytorch_result(model_name, meta_info, checkpoint_path,
                       model_config_name, metric_tolerance, report_dict,
                       logger):
    """Get metric from metafile info of the model.

    Args:
        model_name (str): Name of model.
        meta_info (dict): Metafile info from model's metafile.yml.
        checkpoint_path (Path): Checkpoint path.
        model_config_name (Path):  Model config name for getting meta info
        metric_tolerance (dict):Tolerance for metrics.
        report_dict (dict): Report info dict.
        logger (logging.Logger): Logger.

    Returns:
        Dict: metric info of the model
    """

    if model_config_name.stem not in meta_info:
        return {}

    model_info = meta_info.get(model_config_name.stem, None)

    # get metric
    metric_info = model_info.get('Results', None)
    metric_list = []
    pytorch_metric = dict()
    for metric in metric_info:
        metric_list.append(metric.get('Metrics'))
        pytorch_metric.update(metric.get('Metrics'))

    # update useless metric
    metric_all_list = [str(metric) for metric in metric_tolerance]
    metric_useless = set(metric_all_list) - set(
        [str(metric) for metric in pytorch_metric])
    for metric in metric_useless:
        metric_list.append({metric: '-'})

    # get pytorch fps value
    fps_info = model_info.get('Metadata').get('inference time (ms/im)')
    if fps_info is None:
        fps = '-'
    elif isinstance(fps_info, list):
        fps = fps_info[0].get('value')
    else:
        fps = fps_info.get('value')

    # update report
    dataset_type = ''
    for metric in metric_info:
        dataset_type += f'{metric.get("Dataset")},'

    update_report(
        report_dict=report_dict,
        model_name=model_name,
        model_config=str(model_config_name),
        model_checkpoint_name=str(checkpoint_path),
        dataset=dataset_type,
        backend_name='Pytorch',
        deploy_config='-',
        static_or_dynamic='-',
        conversion_result='-',
        fps=fps,
        metric_info=metric_list,
        test_pass='-')

    logger.info(f'Got {model_config_name} metric: {pytorch_metric}')
    return pytorch_metric


def get_info_from_log_file(info_type, log_path, metric_info=None):
    # get fps from log file
    if log_path.exists():
        with open(log_path, 'r') as f_log:
            lines = f_log.readlines()
    else:
        print(f'{log_path} do not exist !!!')
        lines = []

    if info_type == 'FPS' and len(lines) > 1:
        line_count = 0
        fps_sum = 0.00
        for line in lines[-6:-1]:
            if 'FPS' not in line:
                continue
            line_count += 1
            fps_sum += float(line.split(' ')[-2])
        info_value = f'{fps_sum / line_count:.2f}'
    elif info_type == 'metric' and len(lines) > 1:
        metric_line = lines[-1]
        metric_dict_str = \
            metric_line.replace('\n', '').replace('\r', '').split(' - ')[-1]
        print(f'Got metric_dict = {metric_dict_str}')

        evaluate_result = eval(metric_dict_str)
        if not isinstance(evaluate_result, OrderedDict):
            print(f'Got error metric_dict = {metric_dict_str}')
            return '-'
        print(f'Got metric_eval_name = {metric_info}')
        metric = evaluate_result.get(metric_info, 0.00) * 100
        print(f'Got metric = {metric}')
        info_value = metric
    else:
        info_value = '-'

    return info_value


def get_backend_fps_metric(deploy_cfg_path,
                           model_cfg_path,
                           convert_checkpoint_path,
                           device_type,
                           metric_name,
                           logger,
                           metric_info_dict,
                           metrics_eval_list,
                           pytorch_metric,
                           metric_tolerance,
                           backend_name,
                           metric_useless,
                           convert_result,
                           report_dict,
                           infer_type,
                           log_path
                           ):
    metric_list = []

    result_path = Path(convert_checkpoint_path).with_suffix('.pkl').absolute()

    cmd_str = f'cd {str(Path().cwd())} && ' \
              'python3 tools/test.py ' \
              f'{deploy_cfg_path} ' \
              f'{str(model_cfg_path.absolute())} ' \
              f'--model {str(convert_checkpoint_path)} ' \
              f'--out {str(result_path)} ' \
              f'--metrics {metric_name} ' \
              f'--device {device_type} ' \
              f'--log2file {log_path} ' \
              f'--speed-test'

    logger.info(f'Process cmd = {cmd_str}')

    # Test backend
    shell_res = os.system(cmd_str)
    print(f'Got shell_res = {shell_res}')

    # check if converted successes or not.
    if shell_res != 0:
        fps = '-'
        metric_value = '-'
    else:
        # Got fps from log file
        fps = get_info_from_log_file('FPS', log_path)
        print(f'Got fps = {fps}')

        # Got metric from log file
        metric_eval_name = \
            metric_info_dict.get(metric_name, {}).get('metric_name', '0.00')
        metric_value = get_info_from_log_file('metric', log_path, metric_eval_name)
    print(f'Got metric = {metric_value}')

    metric_name = metric_info_dict.get(metric_name, {}).get('meta_name', None)
    if metric_name is None:
        logger.error(f'metrics_eval_list: {metrics_eval_list} '
                     'has not info name')
    assert metric_name is not None

    metric_list.append({metric_name: metric_value})
    metric_pytorch = pytorch_metric.get(str(metric_name))
    metric_tolerance_value = metric_tolerance.get(metric_name)
    if (metric_value - metric_tolerance_value) <= \
            metric_pytorch <= \
            (metric_value + metric_tolerance_value):
        test_pass = True
    else:
        test_pass = False

    # update useless metric
    for metric in metric_useless:
        metric_list.append({metric: '-'})

    # update the report
    update_report(
        report_dict=report_dict,
        model_name=model_cfg_path.parent.name,
        model_config=str(model_cfg_path),
        model_checkpoint_name=str(convert_checkpoint_path),
        dataset='',
        backend_name=backend_name,
        deploy_config=str(deploy_cfg_path),
        static_or_dynamic=infer_type,
        conversion_result=str(convert_result),
        fps=fps,
        metric_info=metric_list,
        test_pass=str(test_pass))


def get_backend_result(pipeline_info, model_cfg_path,
                       checkpoint_path, work_dir,
                       device_type, pytorch_metric, metric_tolerance,
                       report_dict, test_type, logger,
                       log_path):
    """Convert model to onnx and then get metric.

    Args:
        pipeline_info (dict):  Pipeline info of test yaml.
        model_cfg_path (Path): Model config file path.
        checkpoint_path (Path): Checkpoints path.
        work_dir (Path): A working directory.
        device_type (str): A string specifying device, defaults to 'cuda'.
        pytorch_metric (dict): All pytorch metric info.
        metric_tolerance (dict):Tolerance for metrics.
        report_dict (dict): Report info dict.
        test_type (sgr): Test type. 'precision' or 'convert'.
        logger (logging.Logger): Logger.
        log_path (Path): Path for logger file.
    """

    backend_file_info = {
        'onnxruntime': 'end2end.onnx',
        'tensorrt': 'end2end.engine',
        'torchscript': 'end2end.pt',

        # unknown
        'openvino': '',
        'ncnn': '',
        'pplnn': '',
    }

    metric_info_dict = {
        # mmdet
        'bbox': {
            'meta_name': 'box AP',
            'metric_name': 'bbox_mAP',
        },
        'segm': {
            'meta_name': 'mask AP',
            'metric_name': '?',
        },
        'proposal': {
            'meta_name': 'PQ',
            'metric_name': '?',
        },
    }

    # get backend_test info
    backend_test = pipeline_info.get('backend_test', False)

    # get convert_image info
    input_img_path = \
        pipeline_info.get('convert_image', {}).get('input_img', './tests/data/tiger.jpeg')
    test_img_path = \
        pipeline_info.get('convert_image', {}).get('test_img', None)

    # get sdk_cfg info
    sdk_config = pipeline_info.get('sdk_config', None)
    if sdk_config is not None:
        sdk_config = Path(sdk_config).absolute().resolve()

    metric_name_list = [str(metric) for metric in pytorch_metric]
    assert len(metric_name_list) > 0
    metric_all_list = [str(metric) for metric in metric_tolerance]
    metric_useless = set(metric_all_list) - set(metric_name_list)

    deploy_cfg_path = Path(pipeline_info.get('deploy_config')).absolute().resolve()
    backend_name = str(get_backend(str(deploy_cfg_path)).name).lower()
    infer_type = 'dynamic' if is_dynamic_shape(str(deploy_cfg_path)) else 'static'

    backend_output_path = Path(work_dir). \
        joinpath(Path(checkpoint_path).parent.parent.name,
                 Path(checkpoint_path).parent.name,
                 backend_name,
                 infer_type,
                 Path(checkpoint_path).stem)
    backend_output_path = backend_output_path.absolute().resolve()
    backend_output_path.mkdir(parents=True, exist_ok=True)

    # convert cmd string
    cmd_str = f'cd {str(Path().cwd())} && ' \
              'python3 ./tools/deploy.py ' \
              f'{str(deploy_cfg_path.absolute().resolve())} ' \
              f'{str(model_cfg_path.absolute().resolve())} ' \
              f'{str(checkpoint_path.absolute().resolve())} ' \
              f'{input_img_path} ' \
              f'--work-dir {backend_output_path} ' \
              f'--device {device_type} ' \
              '--log-level INFO'

    if sdk_config is not None:
        cmd_str += ' --dump-info'

    if infer_type == 'dynamic' and test_img_path is not None:
        cmd_str += f' --test-img {test_img_path}'

    logger.info(f'Process cmd = {cmd_str}')

    # Convert the model to specific backend
    shell_res = os.system(cmd_str)
    print(f'Got shell_res = {shell_res}')

    # check if converted successes or not.
    if shell_res == 0:
        convert_result = True
    else:
        convert_result = False
    print(f'Got convert_result = {convert_result}')

    convert_checkpoint_path = \
        backend_output_path.joinpath(backend_file_info.get(backend_name, ''))

    # Test the model
    fps = '-'
    if convert_result and test_type != 'convert':
        # load deploy_cfg
        deploy_cfg, model_cfg = \
            load_config(str(deploy_cfg_path),
                        str(model_cfg_path.absolute()))

        # Get evaluation metric from model config
        metrics_eval_list = model_cfg.evaluation.get('metric', [])
        if isinstance(metrics_eval_list, str):
            # some config is using str only
            metrics_eval_list = [metrics_eval_list]

        assert len(metrics_eval_list) > 0
        print(f'Got metrics_eval_list = {metrics_eval_list}')

        # test the model metric
        for metric_name in metrics_eval_list:
            if backend_test:
                get_backend_fps_metric(deploy_cfg_path=deploy_cfg_path,
                                       model_cfg_path=model_cfg_path,
                                       convert_checkpoint_path=convert_checkpoint_path,
                                       device_type=device_type,
                                       metric_name=metric_name,
                                       logger=logger,
                                       metric_info_dict=metric_info_dict,
                                       metrics_eval_list=metrics_eval_list,
                                       pytorch_metric=pytorch_metric,
                                       metric_tolerance=metric_tolerance,
                                       backend_name=backend_name,
                                       metric_useless=metric_useless,
                                       convert_result=convert_result,
                                       report_dict=report_dict,
                                       infer_type=infer_type,
                                       log_path=log_path
                                       )

            if sdk_config is not None:
                get_backend_fps_metric(deploy_cfg_path=str(sdk_config),
                                       model_cfg_path=model_cfg_path,
                                       convert_checkpoint_path=str(backend_output_path),
                                       device_type=device_type,
                                       metric_name=metric_name,
                                       logger=logger,
                                       metric_info_dict=metric_info_dict,
                                       metrics_eval_list=metrics_eval_list,
                                       pytorch_metric=pytorch_metric,
                                       metric_tolerance=metric_tolerance,
                                       backend_name='SDK',
                                       metric_useless=metric_useless,
                                       convert_result=convert_result,
                                       report_dict=report_dict,
                                       infer_type=infer_type,
                                       log_path=log_path
                                       )
    else:
        metric_list = []

        for metric in metric_name_list:
            metric_list.append({metric: '-'})
        test_pass = True if convert_result else False

        # update useless metric
        for metric in metric_useless:
            metric_list.append({metric: '-'})

        # update the report
        update_report(
            report_dict=report_dict,
            model_name=model_cfg_path.parent.name,
            model_config=str(model_cfg_path),
            model_checkpoint_name=str(checkpoint_path),
            dataset='',
            backend_name=backend_name,
            deploy_config=str(deploy_cfg_path),
            static_or_dynamic=infer_type,
            conversion_result=str(convert_result),
            fps=fps,
            metric_info=metric_list,
            test_pass=str(test_pass))


def save_report(report_info, report_save_path, logger):
    """Convert model to onnx and then get metric.

    Args:
        report_info (dict):  Report info dict.
        report_save_path (Path): Report save path.
        logger (logging.Logger): Logger.
    """
    logger.info(f'Save regression test report '
                f'to {report_save_path}, pls wait...')

    df = pd.DataFrame(report_info)
    df.to_excel(report_save_path)

    logger.info(f'Saved regression test report to {report_save_path}.')


def main():
    args = parse_args()

    set_start_method('spawn')

    logger = get_root_logger(log_level=args.log_level)
    logger.info('Processing regression test.')

    backend_list = args.backends
    if backend_list == ['all']:
        backend_list = [
            'onnxruntime', 'tensorrt', 'openvino', 'ncnn',
            'pplnn', 'torchscript'
        ]
    assert isinstance(backend_list, list)
    logger.info(f'Regression test backend list = {backend_list}')

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    log_path = work_dir.joinpath('regression_test.log').absolute()
    if log_path.exists():
        with open(log_path, 'w') as f_log:
            f_log.write('')  # clear the log file
    logger.info(f'Savine log in {str(log_path)}')

    for deploy_yaml in args.deploy_yml:

        if not Path(deploy_yaml).exists():
            raise FileNotFoundError(f'deploy_yaml {deploy_yaml} not found, '
                                    'please check !')

        with open(deploy_yaml) as f:
            yaml_info = yaml.load(f, Loader=yaml.FullLoader)

        report_save_path = \
            work_dir.joinpath(Path(deploy_yaml).stem + '_report.xlsx')
        report_dict = {
            'model_name': [],
            'model_config': [],
            'model_checkpoint_name': [],
            'dataset': [],
            'backend_name': [],
            'deploy_config': [],
            'static_or_dynamic': [],
            'conversion_result': [],
            'fps': []
        }

        global_info = yaml_info.get('globals')

        for metric_name in global_info.get('metric_tolerance', {}):
            report_dict.update({metric_name: []})
        metric_tolerance = global_info.get('metric_tolerance', {})
        report_dict.update({'test_pass': []})

        models_info = yaml_info.get('models')
        for models in models_info:
            if 'model_configs' not in models:
                print(f'Skip {models.get("name")}')
                continue

            model_metafile_info, checkpoint_save_dir, codebase_dir = \
                get_model_metafile_info(global_info, models, logger)
            for model_config in model_metafile_info:
                logger.info(f'Processing test for {model_config}.py...')

                # get backends info
                pipelines_info = models.get('pipelines', None)
                if pipelines_info is None:
                    continue

                # get model config path
                model_cfg_path = Path(codebase_dir). \
                    joinpath(models.get('codebase_model_config_dir', ''),
                             model_config).with_suffix('.py')
                assert model_cfg_path.exists()

                # get checkpoint path
                checkpoint_name = Path(
                    model_metafile_info.get(model_config).get('Weights')).name
                checkpoint_path = Path(checkpoint_save_dir, checkpoint_name)
                assert checkpoint_path.exists()

                #  Get pytorch from metafile.yml
                pytorch_metric = get_pytorch_result(
                    models.get('name'), model_metafile_info, checkpoint_path,
                    model_cfg_path, metric_tolerance, report_dict, logger)

                for pipeline in pipelines_info:
                    backend_name = get_backend(str(pipeline.get('deploy_config'))).name
                    if str(backend_name).lower() not in backend_list:
                        continue

                    get_backend_result(
                        pipeline, model_cfg_path, checkpoint_path,
                        work_dir, args.device_id, pytorch_metric,
                        metric_tolerance, report_dict, args.test_type,
                        logger, log_path)

        save_report(report_dict, report_save_path, logger)


if __name__ == '__main__':
    main()

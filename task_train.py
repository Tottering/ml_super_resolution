"""
Implement a script that train and validate super-resolution model.
"""
import functools
import importlib
import logging
import os
import pathlib

import numpy as np
import ruamel.yaml as yaml
import tensorflow as tf

import dataset_builder


def resolve_strings(data, experiment_name):
    """
    Replace "{experiment_name}" with experiment_name in each string except
    dictionary key. If a dictionary key contains 'path', resolve its value
    (e.g. expand ~/).

    Arguments:
        data: a dictionary or a list for resolving string values.
        experiment_name: a string to replace "{experiment_name}" within data.
    """
    # NOTE: We expect data is either a list or a dictionary.
    iterator = enumerate(data) if isinstance(data, list) else data.items()

    for key, val in iterator:
        if isinstance(val, list) or isinstance(val, dict):
            resolve_strings(val, experiment_name)
        elif isinstance(val, str):
            val = val.replace('{experiment_name}', experiment_name)

            if isinstance(key, str) and 'path' in key:
                if pathlib.PurePath(val).is_absolute():
                    pass
                elif val.startswith('~'):
                    val = str(pathlib.Path(val).expanduser())
                else:
                    val = str(pathlib.Path(val).resolve())

            data[key] = val


def global_step(context):
    """
    Count the global training step base on current iterations of the most
    active optimizer and the record from checkpoint. Return the global training
    step.

    Arguments:
        context: experiment information in a dictionary.

    Return:
        Adjusted global step.
    """
    strategy = context['strategy']
    step = 0

    # NOTE: optimizer.iterations starts from 0 for each training session (
    #       both on fresh start and load from checkpoint)
    for optimizer in context['optimizers'].values():
        num_iterations = strategy.reduce(
            tf.distribute.ReduceOp.MEAN, optimizer.iterations, axis=None)

        step = max(step, int(num_iterations))

    return step + context['global_step']


def find_latest_checkpoint(path):
    """
    If path is a directory, assume it contains checkpoints and try to locate
    it.

    Arguments:
        path: Path to a checkpoint or a directory contains checkpoints.

    Raises:
        ValueError: If path is a directory but contains no checkpoint.

    Return:
        If path is not a directory, return it directly.
        If path contains no checkpoint, raise an exception.
        Sort the checkpoint and return the newest one (Assume global_step is
        embedded in the file name).
    """
    if not os.path.isdir(path):
        return path

    names = os.listdir(path)
    names = [name for name in names if name.endswith('_checkpoint.yaml')]
    names = sorted(names)

    if names:
        return os.path.join(path, names[-1])
    else:
        raise ValueError(f'Found no checkpoint within {path}')


def load_experiment(path):
    """
    Read experiment information from a yaml file. Return a dictionary contains
    basic configuration of this experiment as the experiment context.

    Arguments:
        path: Path to a fresh experiment yaml or a checkpoint yaml.

    Raises:
        ValueError:
            - if path is invalid.
            - if the experiment has no name.
            - if configuration of summay is missing.

    Return:
        A dictionary contains all information of this training task.
    """
    if not os.path.isfile(path):
        raise ValueError(f'Invalid experiment path: {path}')

    with open(path, 'r') as yaml_file:
        experiment = yaml.YAML(typ='safe').load(yaml_file)

    if 'name' not in experiment:
        raise ValueError('An experiment needs a name.')

    if not experiment.get('summary', {}).get('path', None):
        raise ValueError('An experiment needs a scribe.')

    if not experiment.get('checkpoint', {}).get('path', None):
        raise ValueError('An experiment needs a dir for checkpoint')

    if 'global_step' not in experiment:
        experiment['global_step'] = 0

    resolve_strings(experiment, experiment['name'])

    os.makedirs(experiment['checkpoint']['path'], exist_ok=True)

    scribe = tf.summary.create_file_writer(experiment['summary']['path'])
    logger = logging.getLogger(experiment['name'])

    logger.setLevel(logging.INFO)

    gpus = tf.config.experimental.list_logical_devices('GPU')
    gpus = [gpu.name for gpu in gpus]

    if len(gpus) > 1:
        strategy = tf.distribute.MirroredStrategy(gpus)
    else:
        strategy = tf.distribute.OneDeviceStrategy(gpus[0])

    logger.info('global step: %s', experiment['global_step'])
    logger.info('summary: %s', experiment['summary']['path'])
    logger.info('strategy %s: %s', strategy, gpus)

    # NOTE: Keep the loaded experiment. It can be then saved as checkpoint with
    #       updated information. This way the checkpoint would be in the same
    #       format as a fresh experiment yaml.
    return {
        'experiment': experiment,
        'strategy': strategy,
        'logger': logger,
        'scribe': scribe,
        'global_step': experiment['global_step'],
    }


def build_optimizer(config):
    """
    Build an optimizer from config.

    Arguments:
        config: Information of an optimizer.

    Return:
        A keras optimizer.
    """
    if config['optimizer'].lower() == 'adam':
        optimizer_class = tf.keras.optimizers.Adam

    optimizer_config = {}

    if 'learning_rate' in config:
        optimizer_config['learning_rate'] = config['learning_rate']

    optimizer_config = config.get('config', {}) or optimizer_config

    return optimizer_class.from_config(optimizer_config)


def build_strategic_training_model(
        strategy, base_model, index_inputs, optimizer, num_replicas):
    """
    Make a new model that can be invoked with distribution strategy.

    Arguments:
        strategy: A distribution strategy which will applied to base_model for
            training.
        base_model: A model which we want to train with distributtion strategy.
        index_inputs: List of index. Use it to rearange inputs for the model.
            The dataset may supply data in different order. For example, the
            dataset may supply (sd_images, hd_images) in each iteration while
            base_model accept (hd_images, sd_images), in this case we need
            index_inputs to be [1, 0].
        optimizer:
            The optimizer we use to train base_model.
        num_replicas:
            Number of distribution replicas. We multiple batch size with number
            of replicas and let the strategy shard it.

    Return:
        A new tf.function like base_model but can be trained with strategy.
    """
    # NOTE: Refer to tensorflow distribute_strategy guide.
    #       https://www.tensorflow.org/beta/guide/distribute_strategy#using_tfdistributestrategy_with_custom_training_loops
    #
    #       Using tf.distribute.Strategy with custom training loops
    #
    #       When apply_gradients is called within a distribution strategy
    #       scope, its behavior is modified. Specifically, before applying
    #       gradients on each parallel instance during synchronous training, it
    #       performs a sum-over-all-replicas of the gradients.
    # NOTE: All losses from model must be element-wisely averaged. So all we
    #       have to do for apply_gradients is divieding by number of replicas.
    #       Then apply_gradients will multiply it back by sum-over-all-replicas
    #       of the gradients.
    def train_one_step(inputs):
        new_inputs = [inputs[index] for index in index_inputs]

        with tf.GradientTape() as tape:
            loss = base_model(new_inputs)

            loss = tf.reduce_sum(loss) * (1.0 / num_replicas)

        gradients = tape.gradient(loss, base_model.trainable_variables)

        optimizer.apply_gradients(
            zip(gradients, base_model.trainable_variables))

        return loss

    @tf.function
    def train_one_step_in_graph(data):
        losses = strategy.experimental_run_v2(train_one_step, args=(data,))

        return strategy.reduce(tf.distribute.ReduceOp.MEAN, losses, axis=None)

    return train_one_step_in_graph


def build_strategic_validation_model(
        strategy, base_model, index_inputs, index_hd_images):
    """
    Make a new model that can be invoked with distribution strategy.

    Arguments:
        strategy: A distribution strategy which will applied to base_model for
            validation.
        base_model: A model which we want to validate with distributtion
            strategy.
        index_inputs: List of index. Use it to rearange inputs for the model.
            The dataset may supply data in different order. For example, the
            dataset may supply (sd_images, hd_images) in each iteration while
            base_model accept (hd_images, sd_images), in this case we need
            index_inputs to be [1, 0].
        index_hd_images: Index of hd images in inputs.

    Return:
        A new tf.function like base_model but can be validated with strategy.
    """
    def validate_one_step(inputs):
        new_inputs = [inputs[index] for index in index_inputs]

        hd_images = inputs[index_hd_images]

        outputs = base_model(new_inputs)

        return outputs, hd_images

    @tf.function
    def validate_one_step_in_graph(data):
        return strategy.experimental_run_v2(validate_one_step, args=(data,))

    return validate_one_step_in_graph


def build_datasets(context):
    """
    Build all datasets base on experiment information and keep them in the
    context.

    Arguments:
        context: experiment information in a dictionary.
    """
    def dataset_fn(input_context, dataset):
        return dataset.shard(
            input_context.num_input_pipelines, input_context.input_pipeline_id)

    gpus = tf.config.experimental.list_logical_devices('GPU')
    num_gpus = len(gpus)

    strategy = context['strategy']
    datasets = context['experiment']['datasets']

    context['datasets'] = {}

    for name, dataset in datasets.items():
        # NOTE: Multiple batch_size with number of GPUs then shard for
        #       distribution strategy.
        tf_dataset = dataset_builder.build_dataset(
            subsets=dataset['subsets'],
            batch_size=dataset['batch_size'] * num_gpus,
            sample_rate=dataset['sample_rate'],
            augment=dataset['augment'])

        with strategy.scope():
            tf_dataset = strategy \
                .experimental_distribute_datasets_from_function(
                    functools.partial(dataset_fn, dataset=tf_dataset))

        context['datasets'][name] = iter(tf_dataset)


def build_models(context):
    """
    Build all models base on experiment information and keep them in the
    context.

    Arguments:
        context: experiment information in a dictionary.

    Raises:
        ValueError: if datasets have not been built yet.
    """
    if 'datasets' not in context:
        raise ValueError('build datasets before building models')

    models_config = context['experiment']['models']
    optimizers_config = context['experiment']['optimizers']

    with context['strategy'].scope():
        context['models'] = importlib \
            .import_module(models_config['name']) \
            .build_models(**models_config['parameters'])

    # NOTE: Build optimizers.
    context['optimizers'] = {}
    context['models']['strategic_extensions'] = strategic_extensions = {}
    context['models']['strategic_principals'] = strategic_principals = {}

    gpus = tf.config.experimental.list_logical_devices('GPU')
    num_gpus = len(gpus)

    for optimizer_name, config in optimizers_config.items():
        model_name = config['extension_model']

        model = context['models']['extensions'][model_name]

        dataset = context['datasets'][config['dataset']['name']]

        with context['strategy'].scope():
            context['optimizers'][optimizer_name] = build_optimizer(config)

            strategic_model = build_strategic_training_model(
                context['strategy'],
                model,
                config['dataset']['input_indices'],
                context['optimizers'][optimizer_name],
                num_gpus)

            # NOTE: Call once to force building the model variables so we can
            #       load weights.
            strategic_model(next(dataset))

        strategic_extensions[model_name] = strategic_model

    for validator in context['experiment']['validators']:
        model_name = validator['principal_model']

        model = context['models']['principals'][model_name]

        with context['strategy'].scope():
            strategic_model = build_strategic_validation_model(
                context['strategy'],
                model,
                validator['dataset']['input_indices'],
                validator['dataset']['hd_image_index'])

        strategic_principals[model_name] = strategic_model

    # NOTE: Load weights.
    for name, config in models_config['principals'].items():
        if not os.path.isfile(config.get('path', '') or ''):
            continue

        with context['strategy'].scope():
            context['models']['principals'][name].load_weights(config['path'])


def train(context):
    """
    Iterate through all optimizers and train their associated models for one
    step.

    Arguments:
        context: experiment information in a dictionary.
    """
    step = global_step(context)

    strategic_extensions = context['models']['strategic_extensions']

    for name in context['optimizers']:
        config = context['experiment']['optimizers'][name]

        if step % config['cycle'] != 0:
            continue

        dataset = context['datasets'][config['dataset']['name']]

        model = strategic_extensions[config['extension_model']]

        with context['strategy'].scope():
            loss = model(next(dataset))

        with context['scribe'].as_default():
            tf.summary.scalar(f'loss[{name}]', data=loss, step=step)

        context['logger'].info(f'loss[{name}][{step}]: {loss}')


def validate(context):
    """
    Iterate through all validators and do basic validation for their associated
    models.

    Arguments:
        context: experiment information in a dictionary.
    """
    step = global_step(context)

    strategic_principals = context['models']['strategic_principals']

    for validator in context['experiment']['validators']:
        if step % validator['cycle'] != 0:
            continue

        dataset_name = validator['dataset']['name']

        dataset = context['datasets'][dataset_name]

        model = strategic_principals[validator['principal_model']]

        with context['strategy'].scope():
            resp = model(next(dataset))

        # TODO: Must we be that dirty?
        if hasattr(resp[0], 'values'):
            sr_images = tf.concat(resp[0].values, axis=0)
            hd_images = tf.concat(resp[1].values, axis=0)
        else:
            sr_images = tf.concat(resp[0], axis=0)
            hd_images = tf.concat(resp[1], axis=0)

        psnr = tf.image.psnr(sr_images, hd_images, 2.0)
        psnr = np.mean(psnr)

        ssim = tf.image.ssim(sr_images, hd_images, 2.0)
        ssim = np.mean(ssim)

        hd_image = np.concatenate(hd_images, axis=1)
        sr_image = np.concatenate(sr_images, axis=1)

        summary_image = np.concatenate([hd_image, sr_image], axis=0)
        summary_image = [summary_image * 0.5 + 0.5]

        with context['scribe'].as_default():
            tf.summary.scalar(
                f'psnr[{validator["name"]}]', data=psnr, step=step)
            tf.summary.scalar(
                f'ssim[{validator["name"]}]', data=ssim, step=step)
            tf.summary.image(
                f'hd-sr[{validator["name"]}]', data=summary_image, step=step)

        context['logger'].info(f'psnr[{validator["name"]}][{step}]: {psnr}')
        context['logger'].info(f'ssim[{validator["name"]}][{step}]: {ssim}')


def save(context):
    """
    Save the experiment.

    Arguments:
        context: experiment information in a dictionary.
    """
    experiment = context['experiment']

    step = global_step(context)

    if step % experiment['checkpoint']['cycle'] != 0:
        return

    experiment['global_step'] = step

    # NOTE: Save model weights.
    for model_name, config in experiment['models']['principals'].items():
        name = f'{str(step).rjust(16, "0")}_{model_name}.h5'
        path = os.path.join(experiment['checkpoint']['path'], name)

        context['models']['principals'][model_name].save_weights(path)

        config['path'] = path

    # NOTE: Update optimizers' configs.
    for name, config in experiment['optimizers'].items():
        config['config'] = context['optimizers'][name].get_config()

    # NOTE: Save checkpoint.
    name = f'{str(step).rjust(16, "0")}_checkpoint.yaml'
    path = os.path.join(experiment['checkpoint']['path'], name)

    with open(path, 'w') as yaml_file:
        # NOTE: Add a representer to handle numpy.float32 as float.
        def represent_numpy_float32(representer, data):
            return representer.represent_float(data)

        dumper = yaml.YAML()

        dumper.representer.add_representer(np.float32, represent_numpy_float32)
        dumper.dump(experiment, stream=yaml_file)


def train_validate_save(experiment_path):
    """
    Build the experiment and train the models.

    Arguments:
        experiment_path: path to a yaml which contains information of the
            experiment.
    """
    context = load_experiment(experiment_path)

    build_datasets(context)
    build_models(context)

    while True:
        train(context)
        validate(context)
        save(context)


if __name__ == '__main__':
    import sys

    if len(sys.argv) != 2:
        raise ValueError('Usage: python task_train.py experiment_ooxx.json')

    # NOTE: Remove comments to test mirrored distribution strategy with single
    #       GPU.
#   gpus = tf.config.experimental.list_physical_devices('GPU')

#   tf.config.experimental.set_virtual_device_configuration(
#       gpus[0],
#       [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=5120),
#        tf.config.experimental.VirtualDeviceConfiguration(memory_limit=5120)])

    experiment_path = find_latest_checkpoint(sys.argv[1])

    train_validate_save(experiment_path)

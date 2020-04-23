import os
import sys
from tqdm.auto import tqdm
import datetime
import numpy as np
from pathlib import Path
from copy import deepcopy
import ruamel.yaml as yaml

from moseq2_extract.extract.proc import apply_roi
from moseq2_extract.extract.extract import extract_chunk
from moseq2_extract.io.video import load_movie_data, write_frames_preview

def process_extract_batches(f, input_file, config_data, bground_im, roi, scalars, frame_batches, first_frame_idx, \
                            true_depth, tar, strel_tail, strel_min, output_dir, output_filename):
    '''
    Compute extracted frames and save them to h5 files and avi files.
    Parameters
    ----------
    f (h5py.File): opened h5 file to write extracted batches to
    input_file (str): path to depth file
    config_data (dict): dictionary containing extraction parameters (autogenerated)
    bground_im (2d numpy array):  r x c, background image
    roi (2d numpy array):  r x c, roi image
    scalars (list): list of keys to scalar attribute values
    frame_batches (list): list of batches of frames to serially process.
    first_frame_idx (int): index of starting frame.
    true_depth (float): computed detected true depth.
    tar (bool): compressed file indicator.
    strel_tail (cv2.StructuringElement): Element for tail filtering.
    strel_min (int): minimum kernel size
    output_dir (str): path to output directory that contains the extracted data, e.g. (proc/).
    output_filename (str): name of h5 file containing extraction data, e.g. (results_00).
    Returns
    -------
    video_pipe (bool): boolean for whether function is done writing to video file.
    '''

    video_pipe = None
    tracking_init_mean = None
    tracking_init_cov = None

    for i, frame_range in enumerate(tqdm(frame_batches, desc='Processing batches')):
        raw_frames = load_movie_data(input_file, [f + first_frame_idx for f in frame_range], tar_object=tar)
        raw_frames = bground_im - raw_frames
        # raw_frames[np.logical_or(raw_frames < min_height, raw_frames > max_height)] = 0
        raw_frames[raw_frames < config_data['min_height']] = 0
        if config_data['dilate_iterations'] == 1:
            raw_frames[raw_frames > config_data['max_height']] = config_data['max_height']
        else:
            raw_frames[raw_frames > config_data['max_height']] = 0
        raw_frames = raw_frames.astype(config_data['frame_dtype'])
        raw_frames = apply_roi(raw_frames, roi)

        results = extract_chunk(raw_frames,
                                use_em_tracker=config_data['use_tracking_model'],
                                strel_tail=strel_tail,
                                strel_min=strel_min,
                                iters_tail=config_data['tail_filter_iters'],
                                iters_min=config_data['cable_filter_iters'],
                                prefilter_space=config_data['spatial_filter_size'],
                                prefilter_time=config_data['temporal_filter_size'],
                                min_height=config_data['min_height'],
                                max_height=config_data['max_height'],
                                flip_classifier=config_data['flip_classifier'],
                                flip_smoothing=config_data['flip_classifier_smoothing'],
                                crop_size=config_data['crop_size'],
                                frame_dtype=config_data['frame_dtype'],
                                mask_threshold=config_data['tracking_model_mask_threshold'],
                                tracking_ll_threshold=config_data['tracking_model_ll_threshold'],
                                tracking_segment=config_data['tracking_model_segment'],
                                tracking_init_mean=tracking_init_mean,
                                tracking_init_cov=tracking_init_cov,
                                true_depth=true_depth,
                                progress_bar=False,
                                centroid_hampel_span=config_data['centroid_hampel_span'],
                                centroid_hampel_sig=config_data['centroid_hampel_sig'],
                                angle_hampel_span=config_data['angle_hampel_span'],
                                angle_hampel_sig=config_data['angle_hampel_sig'],
                                model_smoothing_clips=config_data['model_smoothing_clips'],
                                tracking_model_init=config_data['tracking_model_init'])

        # if desired, write out a movie

        if i > 0:
            offset = config_data['chunk_overlap']
        else:
            offset = 0

        if config_data['use_tracking_model']:
            results['mask_frames'][results['depth_frames'] < config_data['min_height']] = config_data[
                'tracking_model_ll_clip']
            results['mask_frames'][results['mask_frames'] < config_data['tracking_model_ll_clip']] = config_data[
                'tracking_model_ll_clip']
            tracking_init_mean = results['parameters']['mean'][-(config_data['chunk_overlap'] + 1)]
            tracking_init_cov = results['parameters']['cov'][-(config_data['chunk_overlap'] + 1)]

        frame_range = frame_range[offset:]

        for scalar in scalars:
            f[f'scalars/{scalar}'][frame_range] = results['scalars'][scalar][offset:, ...]

        f['frames'][frame_range] = results['depth_frames'][offset:, ...]
        f['frames_mask'][frame_range] = results['mask_frames'][offset:, ...]

        if config_data['flip_classifier']:
            f['metadata/extraction/flips'][frame_range] = results['flips'][offset:]

        nframes, rows, cols = raw_frames[offset:, ...].shape
        output_movie = np.zeros((nframes, rows + config_data['crop_size'][0], cols + config_data['crop_size'][1]),
                                'uint16')
        output_movie[:, :config_data['crop_size'][0], :config_data['crop_size'][1]] = results['depth_frames'][offset:,
                                                                                      ...]
        output_movie[:, config_data['crop_size'][0]:, config_data['crop_size'][1]:] = raw_frames[offset:, ...]

        video_pipe = write_frames_preview(
            os.path.join(output_dir, f'{output_filename}.mp4'), output_movie,
            pipe=video_pipe, close_pipe=False, fps=config_data['fps'],
            frame_range=[f + first_frame_idx for f in frame_range],
            depth_max=config_data['max_height'], depth_min=config_data['min_height'])

    return video_pipe


def run_local_extract(to_extract, params, prefix, skip_extracted, output_directory):
    '''
    Runs the extract command on given list of sessions to extract on local platform.
    Parameters
    ----------
    to_extract (list): list of paths to files to extract
    params (dict): dictionary of ROI metadata from config file.
    prefix (str): prefix to CLI extraction command.
    skip_extracted (bool): Whether to skip already extracted session.
    output_directory (str): path to preferred output directory.
    Returns
    -------
    None
    '''

    # make the temporary directory if it doesn't already exist
    temp_storage = Path('/tmp/')
    temp_storage.mkdir(parents=True, exist_ok=True)

    suffix = '_{:%Y-%m-%d_%H-%M-%S}'.format(datetime.datetime.now())
    config_store = temp_storage / f'job_config{suffix}.yaml'

    with config_store.open('w') as f:
        yaml.safe_dump(params, f)

    for i, ext in enumerate(to_extract):

        base_command = ''

        if prefix is not None:
            base_command += '{}; '.format(prefix)

        if len(params['bg_roi_index']) > 1:
            base_command += 'moseq2-extract find-roi --config-file {} {}; '.format(
                config_store, ext)

        for roi in params['bg_roi_index']:
            roi_config = deepcopy(params)
            roi_config['bg_roi_index'] = roi
            roi_config_store = os.path.join(
                temp_storage, 'job_config{}_roi{:d}.yaml'.format(suffix, roi))
            with open(roi_config_store, 'w') as f:
                yaml.safe_dump(roi_config, f)

            if output_directory is None:
                base_command += 'moseq2-extract extract --config-file {} --bg-roi-index {:d} {}; ' \
                    .format(roi_config_store, roi, ext)
            else:
                base_command += 'moseq2-extract extract --output-dir {} --config-file {} --bg-roi-index {:d} {}; ' \
                    .format(output_directory, roi_config_store, roi, ext)
            try:
                from moseq2_extract.gui import extract_command
                extract_command(ext, str(to_extract[i].replace(ext, 'proc/')), roi_config_store, skip=skip_extracted)
            except:
                print('Unexpected error:', sys.exc_info())
                print('could not extract', to_extract[i])


def run_slurm_extract(to_extract, params, partition, prefix, escape_path, skip_extracted, output_directory):
    '''
    Runs the extract command on given list of sessions to extract on SLURM platform.
    Parameters
    ----------
    to_extract (list): list of paths to files to extract
    params (dict): dictionary of ROI metadata from config file.
    partition (str): name of slurm partition to use
    prefix (str): prefix to CLI extraction command.
    escape_path (function): gets path to return to original base directory
    skip_extracted (bool): Whether to skip already extracted session.
    output_directory (str): path to preferred output directory.
    Returns
    -------
    None
    '''

    # make the temporary directory if it doesn't already exist
    temp_storage = Path('/tmp/')
    temp_storage.mkdir(parents=True, exist_ok=True)

    suffix = '_{:%Y-%m-%d_%H-%M-%S}'.format(datetime.datetime.now())
    config_store = temp_storage / f'job_config{suffix}.yaml'

    with config_store.open('w') as f:
        yaml.safe_dump(params, f)

    for i, ext in enumerate(to_extract):

        ext = escape_path(ext)
        base_command = 'sbatch -n {:d} --mem={} -p {} -t {} --wrap "' \
            .format(params['cores'], params['memory'], partition, params['wall_time'])
        if prefix is not None:
            base_command += f'{prefix}; '

        if len(params['bg_roi_index']) > 1:
            base_command += 'moseq2-extract find-roi --config-file {} {}; '.format(
                config_store, ext)

        for roi in params['bg_roi_index']:
            roi_config = deepcopy(params)
            roi_config['bg_roi_index'] = roi
            roi_config_store = escape_path(os.path.join(
                temp_storage, 'job_config{}_roi{:d}.yaml'.format(suffix, roi)))
            with open(roi_config_store, 'w') as f:
                yaml.safe_dump(roi_config, f)

            if output_directory is None:
                base_command += 'moseq2-extract extract --config-file {} --bg-roi-index {:d} {}; ' \
                    .format(roi_config_store, roi, ext)
            else:
                base_command += 'moseq2-extract extract --output-dir {} --config-file {} --bg-roi-index {:d} {}; ' \
                    .format(output_directory, roi_config_store, roi, ext)
            try:
                from moseq2_extract.gui import extract_command
                extract_command(ext, str(to_extract[i].replace(ext, 'proc/')), roi_config_store, skip=skip_extracted)
            except:
                print('Unexpected error:', sys.exc_info()[0])
                print('could not extract', to_extract[i])

        base_command += '"'
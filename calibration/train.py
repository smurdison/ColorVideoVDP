import argparse
import logging
import numpy as np
import os
import pandas as pd
import pycvvdp
import sys
import torch
from torch.utils.tensorboard import SummaryWriter
import torchmetrics
import tqdm

import data
from extract_features import read_args_from_file

optimizers = {'adam': torch.optim.Adam}

def get_args():
    parser = argparse.ArgumentParser('Calibrate cvvdp parameters for a new dataset')
    parser.add_argument('quality_file', help='Path to .csv file containinf quality scores.')
    parser.add_argument('-s', '--split-column', default='ref', help='Column name for train-test split.')
    parser.add_argument('-r', '--train-ratio', type=int, choices=range(100), default=80, help='Ratio of training split.')
    parser.add_argument('-i', '--id-column', default=None, help='Column name for unique per-row ID.')
    parser.add_argument('--seed', type=int, default=0, help='Random seed for reproducible splits.')
    parser.add_argument('--masking', default='base', choices=['base', 'mlp'], help='Per-frame masking model.')
    parser.add_argument('--pooling', default='base', choices=['base', 'lstm', 'gru'], help='Reduction method used to pool per-frame features.')
    parser.add_argument('--ckpt', default=None, help='PyTorch checkpoint to retrieve weights/parameters.')
    parser.add_argument('-f', '--features-suffix', default=None, help='suffix to add add to the features diretory name.')
    parser.add_argument('-c', '--config-dir', default=None, help='Metric config dir.')
    parser.add_argument('--gpu', type=int,  default=0, help='Select which GPU to use (e.g. 0), default is GPU 0. Pass -1 to run on the CPU.')
    parser.add_argument('--resample-bands', action='store_true', default=False)
    parser.add_argument('-v', '--verbose', action='store_true', default=False)

    # Training args
    parser.add_argument('-b', '--batch', default=4, help='Batch-size during training.')
    parser.add_argument('-n', '--num-workers', default=1, help='Number of CPU workers for data loading.')
    parser.add_argument('-o', '--optimizer', default='adam', choices=optimizers.keys(), help='Optimizer for training.')
    parser.add_argument('-lr', '--learning-rate', default=1e-3, help='Optimizer learning rate.')
    parser.add_argument('-e', '--num-epochs', default=50, help='Total number of training epochs.')
    parser.add_argument('-l', '--log-dir', default='logs', help='Directory to log intermediate loss and metrics.')
    parser.add_argument('--val-epoch', type=int, default=1, help='Number of epochs between validation steps.')

    args = parser.parse_args()

    # Update config from file
    num_skip = read_args_from_file(args)
    args = parser.parse_args()
    quality_table = pd.read_csv(args.quality_file, skiprows=num_skip)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format='[%(levelname)s] %(message)s', level=level)

    if not args.config_dir is None:
        pycvvdp.utils.config_files.set_config_dir(args.config_dir)
        pfile = os.path.join(args.config_dir, "cvvdp_parameters.json")
        if os.path.isfile( pfile ):
            logging.info( f"Using metric parameter file {pfile}")
        else:
            logging.error( f"Cannot find the parameter file {pfile}")
            sys.exit(-1)

    return args, quality_table

def main():
    args, quality_table = get_args()

    if args.gpu >= 0 and torch.cuda.is_available():
        device = torch.device('cuda:' + str(args.gpu))
    else:
        device = torch.device('cpu')

    if args.masking == 'base' and args.pooling == 'base':
        metric = pycvvdp.cvvdp(quiet=True, device=device, temp_padding='replicate')
        # params = [metric.ch_weights, metric.baseband_weight, metric.beta_sch, metric.beta_tch, metric.beta_t]     # betas are int
        params = [metric.ch_weights, metric.baseband_weight]
    else:
        metric = pycvvdp.cvvdp_nn(quiet=True, device=device, temp_padding='replicate', masking=args.masking, pooling=args.pooling, ckpt=args.ckpt)
        # TODO: params
    params.extend([metric.jod_a, metric.jod_exp])
    for p in params:
        p.requires_grad = True

    assert args.split_column in quality_table.columns, f'Split column "{args.split_column}" not found'
    np.random.seed(args.seed)
    unique_cond = np.random.permutation(quality_table[args.split_column].unique())
    train_cond = unique_cond[:(len(unique_cond)*args.train_ratio)//100]
    train_table = quality_table[quality_table[args.split_column].isin(train_cond)]
    test_table = pd.concat([quality_table, train_table]).drop_duplicates(keep=False)    # difference of 2 dataframes

    # Dataloaders
    ft_path = 'features' if args.features_suffix is None else 'features_' + args.features_suffix
    train_loader, val_loader = data.get_loaders(ft_path, train_table, test_table, args.resample_bands, args.batch, args.num_workers)

    # PyTorch training setup
    opt = optimizers[args.optimizer](params, lr=args.learning_rate)
    loss_mse = torch.nn.MSELoss()
    metric_mse = torchmetrics.MeanSquaredError().to(device)
    metric_pearson = torchmetrics.PearsonCorrCoef().to(device)
    metric_spearman = torchmetrics.SpearmanCorrCoef().to(device)
    writer = SummaryWriter(args.log_dir)

    # Main training loop
    for epoch in tqdm.trange(args.num_epochs):
        prog_bar = tqdm.tqdm(train_loader, leave=False)
        for i, batch in enumerate(prog_bar):
            opt.zero_grad()
            jod_hat = []
            for qpc, bb, _ in zip(*batch):
                jod_hat.append(metric.do_pooling_and_jods(qpc.to(device), bb.to(device)))
            jod_hat = torch.stack(jod_hat)

            jod = batch[-1].to(device)
            loss = loss_mse(jod_hat, jod)
            loss.backward()
            opt.step()

            # Log training loss
            global_step = epoch * len(train_loader) + i
            writer.add_scalar('train/loss', loss, global_step)
            prog_bar.set_description(f'loss={loss.item():.3f}')
        
        # Validation
        if epoch % args.val_epoch == 0:
            for i, batch in enumerate(tqdm.tqdm(val_loader, leave=False)):
                jod_hat = []
                for qpc, bb, _ in zip(*batch):
                    jod_hat.append(metric.do_pooling_and_jods(qpc.to(device), bb.to(device)))
                jod_hat = torch.stack(jod_hat)

                jod = batch[-1].to(device)
                metric_mse.update(jod_hat, jod)
                metric_pearson.update(jod_hat, jod)
                metric_spearman.update(jod_hat, jod)

            rmse = torch.sqrt(metric_mse.compute()); metric_mse.reset()
            pearson_rho = metric_pearson.compute(); metric_pearson.reset()
            spearman_rho = metric_spearman.compute(); metric_spearman.reset()
            writer.add_scalar('val/rmse', rmse, epoch)
            writer.add_scalar('val/pearson', pearson_rho, epoch)
            writer.add_scalar('val/spearman', spearman_rho, epoch)

    writer.flush()
    writer.close()

if __name__ == '__main__':
    main()

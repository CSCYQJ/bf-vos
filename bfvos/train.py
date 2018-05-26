import argparse
import os
import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

from bfvos.dataset import davis
from bfvos.model import network, loss, config
import logging
import time
import json

from tqdm import tqdm
from tensorboardX import SummaryWriter
from torchnet.meter import MovingAverageValueMeter

# Logging setup
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Set paths
root_dir = os.path.join('bfvos')
model_dir = os.path.join(root_dir, 'model')
training_dir = os.path.join(root_dir, 'training')
pretrained_dir = os.path.join(model_dir, 'pretrained')
deeplab_resnet_pre_trained_path = os.path.join(pretrained_dir, 'deeplabv2_resnet_pretrained_compatible.pth')
checkpoint_dir = os.path.join(training_dir, 'checkpoints')
config_save_dir = os.path.join(training_dir, 'configs')
tensorboard_save_dir = os.path.join(training_dir, 'tensorboard_logs')

os.makedirs(checkpoint_dir, exist_ok=True)
os.makedirs(config_save_dir, exist_ok=True)
os.makedirs(tensorboard_save_dir, exist_ok=True)

# Pytorch configs
torch.set_default_tensor_type(config.DEFAULT_TENSOR_TYPE)
torch.set_default_dtype(config.DEFAULT_DTYPE)

# select which GPU, -1 if CPU
has_cuda = False  # default
gpu_id = 0
if torch.cuda.is_available():
    has_cuda = True
    logger.info('Using GPU: {} '.format(gpu_id))
device = torch.device("cuda:{}".format(gpu_id) if has_cuda else "cpu")

seed = None
if seed is not None:
    np.random.seed(seed)
    torch.manual_seed(seed)


def parse_args():
    parser = argparse.ArgumentParser()
    # Model configuration
    parser.add_argument('-i', '--image-dims', nargs=2, type=int, help='Input image dimensions as <width>, <height>',
                        metavar=('width', 'height'), default=[256, 256])
    parser.add_argument('-e', '--embedding-vector-dims', type=int, default=128, help='Embedding vector dimensions')
    # Intervals
    parser.add_argument('-l', '--log-interval', type=int, default=10)
    parser.add_argument('-c', '--checkpoint-interval', type=int, default=10)
    # Training parameters
    parser.add_argument('-n', '--num-epochs', type=int, default=1)
    parser.add_argument('-r', '--learning-rate', type=float, default=0.01)
    parser.add_argument('-m', '--momentum', type=float, default=0.1)
    # num_anchor_sample_points = 256  # according to paper
    parser.add_argument('-a', '--alpha', type=float, default=1.0, help='slack variable for loss')
    parser.add_argument('--num-val-samples', type=int, default=10, help='number of validation samples to evaluate')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print debug messages')
    return parser.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    train_data_source = davis.DavisDataset(base_dir=os.path.join(root_dir, 'dataset', 'DAVIS'),
                                           image_size=args.image_dims, year=2016, phase='train',
                                           transform=davis.ToTensor())
    train_triplet_sampler = davis.TripletSampler(dataset=train_data_source, randomize=True)
    train_data_loader = DataLoader(dataset=train_data_source, batch_sampler=train_triplet_sampler)

    val_data_source = davis.DavisDataset(base_dir=os.path.join(root_dir, 'dataset', 'DAVIS'),
                                         image_size=args.image_dims, year=2016, phase='val',
                                         transform=davis.ToTensor())
    val_triplet_sampler = davis.TripletSampler(dataset=val_data_source, randomize=True)
    val_data_loader = DataLoader(dataset=val_data_source, batch_sampler=val_triplet_sampler)

    model = network.BFVOSNet(embedding_vector_dims=args.embedding_vector_dims)
    loss_fn = loss.MinTripletLoss(alpha=args.alpha)
    val_loss_fn = loss.validation_loss
    if has_cuda:
        model = model.cuda()
        loss_fn = loss_fn.cuda()
        loss_fn.to(device)
        logger.debug("Model and loss function moved to CUDA")

    # Load pre-trained model
    if has_cuda:
        model.load_state_dict(
            torch.load(deeplab_resnet_pre_trained_path, map_location=lambda storage, loc: storage.cuda(gpu_id)))
    else:
        model.load_state_dict(torch.load(deeplab_resnet_pre_trained_path))
    logger.info("Loaded DeepLab ResNet from {}".format(deeplab_resnet_pre_trained_path))
    # Load to appropriate device and set to training mode but freeze feature extraction layer
    model.to(device).train()
    model.freeze_feature_extraction()

    # Initialize optimizer to train only the unfrozen layers
    optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=args.learning_rate,
                          momentum=args.momentum)

    # Initialize meter and writer
    loss_meter = MovingAverageValueMeter(20)
    summary_writer = SummaryWriter(tensorboard_save_dir)

    # Train
    for epoch in tqdm(range(args.num_epochs)):
        logger.info("Epoch {}/{}".format(epoch + 1, args.num_epochs))
        train(epoch, train_data_loader, model, loss_fn, optimizer, loss_meter, summary_writer, args.log_interval,
              args.checkpoint_interval)
        validate(epoch, val_data_loader, model, val_loss_fn, loss_meter, summary_writer, args.log_interval,
                 num_val_samples_to_evaluate=args.num_val_samples)

    # Save final model
    model.eval().cpu()
    save_model_filename = "epoch_{}_{}.model".format(args.num_epochs,
                                                     str(time.time()).replace(" ", "_").replace(".", "_"))
    save_model_path = os.path.join(model_dir, save_model_filename)
    torch.save(model.state_dict(), save_model_path)
    logger.info("Model saved to {}".format(save_model_filename))

    training_config_save_path = os.path.join(config_save_dir, save_model_filename.replace('.model', '.json'))
    training_config = vars(args)
    training_config['device'] = str(torch.device)
    with open(training_config_save_path, 'w') as f:
        json.dump(training_config, f)
        logger.info("Training config saved to {}".format(training_config_save_path))


def create_triplet_pools(sample, embeddings):
    """
    Fast vectorized ops to create lots of point-wise triplets from a data loader's sample (3 frames) and embeddings
    :param sample:
    :param embeddings:
    :return:
    """
    embedding_a = embeddings[0]
    embedding_f1 = embeddings[1]
    embedding_f2 = embeddings[2]
    embedding_pool_points = torch.cat([embedding_f1, embedding_f2], 2)  # horizontally stacked frame1 and frame 2
    # embedding_a/p/n is of shape (128, w/8, h/8)

    # TODO: Randomly sample anchor points from anchor frame
    # For now, use all anchor points in the image
    if has_cuda:
        anchor_points = torch.cuda.ByteTensor(sample['annotation'][0])  # all anchor points
    else:
        anchor_points = torch.ByteTensor(sample['annotation'][0])  # all anchor points

    fg_anchor_indices = torch.nonzero(anchor_points)
    bg_anchor_indices = torch.nonzero(anchor_points == 0)

    if fg_anchor_indices.numel() == 0 or bg_anchor_indices.numel() == 0:
        return None

    # all_pool_points is a binary tensor of shape (w/8, h/8).
    # For any index in all_pool_points, if it 1 => it is a foreground pixel
    all_pool_points = torch.cat([sample['annotation'][1], sample['annotation'][2]], 1)
    fg_pool_indices = torch.nonzero(all_pool_points)
    bg_pool_indices = torch.nonzero(all_pool_points == 0)

    if fg_pool_indices.numel() == 0 or bg_pool_indices.numel() == 0:
        return None
    fg_embedding_a = torch.cat([embedding_a[:, x, y].unsqueeze(0) for x, y in fg_anchor_indices])
    bg_embedding_a = torch.cat([embedding_a[:, x, y].unsqueeze(0) for x, y in bg_anchor_indices])

    # Compute loss for all foreground anchor points
    # For foreground anchor points,
    # positive pool: all foreground points in all_pool_points
    # negative pool: all background points in all_pool_points
    fg_positive_pool = torch.cat([embedding_pool_points[:, x, y].unsqueeze(0) for x, y in fg_pool_indices])
    bg_positive_pool = torch.cat([embedding_pool_points[:, x, y].unsqueeze(0) for x, y in bg_pool_indices])

    fg_negative_pool = bg_positive_pool
    bg_negative_pool = fg_positive_pool

    return [fg_embedding_a, fg_positive_pool, fg_negative_pool, bg_embedding_a, bg_positive_pool, bg_negative_pool]


def train(epoch, data_loader, model, loss_fn, optimizer, loss_meter, summary_writer, log_interval, checkpoint_interval):
    agg_fg_loss = 0.
    agg_bg_loss = 0.
    for idx, sample in enumerate(data_loader):
        if has_cuda:
            # move input tensors to gpu
            sample['image'] = sample['image'].to(device=device, dtype=config.DEFAULT_DTYPE)
            sample['annotation'] = sample['annotation'].to(device=device)
        sample_frames = sample['image']
        embeddings = model(sample_frames)

        triplet_pools = create_triplet_pools(sample, embeddings)

        if triplet_pools is None:
            # Skip because not enough triplet samples were generated (possibly due to downsampled/low-res ground truth)
            logger.debug("Skipping iteration {}".format(idx + 1))
            continue
        else:
            fg_embedding_a, fg_positive_pool, fg_negative_pool, bg_embedding_a, bg_positive_pool, bg_negative_pool = triplet_pools

        fg_loss = loss_fn(fg_embedding_a, fg_positive_pool, fg_negative_pool)
        bg_loss = loss_fn(bg_embedding_a, bg_positive_pool, bg_negative_pool)
        final_loss = fg_loss + bg_loss

        # Backpropagation
        optimizer.zero_grad()
        final_loss.backward()
        optimizer.step()

        # Logging

        agg_fg_loss += fg_loss.item()
        agg_bg_loss += bg_loss.item()
        if (idx + 1) % log_interval == 0:
            logger.info("Epoch: {}, Batch: {}".format(epoch + 1, idx + 1))
            logger.info("Avg FG Loss: {}, Avg BG Loss: {}, Avg Total Loss: {}".format(agg_fg_loss / (idx + 1),
                                                                                      agg_bg_loss / (idx + 1),
                                                                                      (agg_fg_loss + agg_bg_loss) / (
                                                                                          idx + 1)))
            summary_writer.add_scalar('train_loss', loss_meter.value()[0], idx + 1)
            for i, o in enumerate(optimizer.param_groups):
                summary_writer.add_scalar('train_lr_group{}'.format(i), o['lr'], idx + 1)
                # for name, param in model.named_parameters():
                #     name = name.replace('.', '/')
                #     summary_writer.add_histogram(name, param, idx + 1, bins="auto")
                # if param.requires_grad:
                #     summary_writer.add_histogram(name + '/grad', param.grad, idx + 1, bins="auto")

        if (idx + 1) % checkpoint_interval == 0:
            model.eval().cpu()
            ckpt_filename = "ckpt_epoch_{}_batch_id_{}.pth".format(epoch + 1, idx + 1)
            ckpt_path = os.path.join(checkpoint_dir, ckpt_filename)
            torch.save(model.state_dict(), ckpt_path)
            logger.info("Checkpoint saved at {}".format(ckpt_filename))
            model.to(device).train()
            model.freeze_feature_extraction()


def validate(epoch, data_loader, model, val_loss, loss_meter, summary_writer, log_interval,
             num_val_samples_to_evaluate=-1):
    model.eval()
    with torch.no_grad():
        agg_fg_loss = 0.
        agg_bg_loss = 0.
        for idx, sample in enumerate(data_loader):
            if has_cuda:
                # move input tensors to gpu
                sample['image'] = sample['image'].to(device=device, dtype=config.DEFAULT_DTYPE)
                sample['annotation'] = sample['annotation'].to(device=device)

            sample_frames = sample['image']
            embeddings = model(sample_frames)

            fg_embedding_a, fg_positive_pool, fg_negative_pool, bg_embedding_a, bg_positive_pool, bg_negative_pool = create_triplet_pools(
                sample, embeddings)

            fg_loss = val_loss(fg_embedding_a, fg_positive_pool, fg_negative_pool)
            bg_loss = val_loss(bg_embedding_a, bg_positive_pool, bg_negative_pool)

            agg_fg_loss += fg_loss.item()
            agg_bg_loss += bg_loss.item()
            if (idx + 1) % log_interval == 0 or idx == num_val_samples_to_evaluate:
                logger.info("Epoch: {}, Batch: {}".format(epoch + 1, idx + 1))
                logger.info(
                    "Avg FG Val Loss: {}, Avg BG Val Loss: {}, Avg Total Val Loss: {}".format(agg_fg_loss / (idx + 1),
                                                                                              agg_bg_loss / (idx + 1),
                                                                                              (
                                                                                                  agg_fg_loss + agg_bg_loss) / (
                                                                                                  idx + 1)))
                summary_writer.add_scalar('val_loss', loss_meter.value()[0], idx + 1)

            if idx == num_val_samples_to_evaluate:
                break


if __name__ == "__main__":
    main()

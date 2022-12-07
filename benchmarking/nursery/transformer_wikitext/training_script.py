# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
# coding: utf-8
import argparse
import os
import time

# import yaml

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

try:
    from apex import amp
except:
    print("Failed to import apex. You can still train with --precision {float|double}.")

from pathlib import Path

import data
import model

from syne_tune.report import Reporter
from syne_tune.config_space import loguniform, uniform
from syne_tune.constants import ST_CHECKPOINT_DIR

report = Reporter()


_config_space = {
    "lr": loguniform(1e-6, 1e-3),
    "dropout": uniform(0, 0.99),
}


def get_batch(source, i, bptt):
    seq_len = min(bptt, len(source) - 1 - i)
    data = source[i : i + seq_len]
    target = source[i + 1 : i + 1 + seq_len].view(-1)
    return data, target


def batchloader(train_data, bptt):
    for batch, i in enumerate(range(0, train_data.size(0) - 1, bptt)):
        yield get_batch(train_data, i, bptt)


def batchify(data, bsz, device):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data.to(device)


def setprec(t, precision):
    if precision == "half":
        # do nothing since this is handled by AMP
        return t
    elif precision == "float":
        return t.float()
    elif precision == "double":
        return t.double()
    else:
        raise ValueError(f"invalid precision string {args.precision}")


DATASET_PATH = "https://raw.githubusercontent.com/pytorch/examples/master/word_language_model/data/wikitext-2/"


def download_data(root):
    import urllib

    path = os.path.join(root, "wikitext-2")
    for fname in ("train.txt", "valid.txt", "test.txt"):
        fh = os.path.join(path, fname)
        if not os.path.exists(fh):
            os.makedirs(path, exist_ok=True)
            urllib.request.urlretrieve(DATASET_PATH + fname, fh)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="""
    PyTorch Wikitext-2 Transformer Language Model
    """,
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("--n-workers", type=int, default=2, metavar="W")
    parser.add_argument("--use-cuda", type=bool, default=True)

    # input data and model directories
    parser.add_argument(
        "--input-data-dir",
        type=str,
        default="./",
        help="location of the data corpus",
    )
    parser.add_argument("--output-data-dir", type=str, default="./output")
    parser.add_argument("--module-name", type=str, default="standard")
    parser.add_argument("--bias", type=bool, default=False, help="use bias")
    parser.add_argument("--d-model", type=int, default=256, help="width of the model")
    parser.add_argument(
        "--ffn-ratio", type=int, default=1, help="the ratio of d_ffn to d_model"
    )
    parser.add_argument("--nlayers", type=int, default=2, help="number of layers")
    parser.add_argument(
        "--nhead",
        type=int,
        default=2,
        help="the number of heads in the encoder/decoder of the transformer model",
    )
    parser.add_argument("--lr", type=float, default=0.001, help="initial learning rate")
    parser.add_argument("--momentum", type=float, default=0, help="momentum")
    parser.add_argument(
        "--output-mult",
        type=float,
        default=1,
        help="output is multiplied by sqrt(output_mult/d_model)",
    )
    parser.add_argument(
        "--input-mult",
        type=float,
        default=1,
        help="input is multiplied by sqrt(input_mult*d_model)",
    )
    parser.add_argument(
        "--attn-mult",
        type=float,
        default=1,
        help="attn is multiplied by sqrt(attn_mult)/head_dim",
    )
    parser.add_argument("--optimizer-name", default="sgd", choices=["sgd", "adam"])
    parser.add_argument(
        "--init-var",
        type=float,
        default=1,
        help="weights are initialized with variance init_var/ninp",
    )
    parser.add_argument("--clip", type=float, default=0.25, help="gradient clipping")
    parser.add_argument("--epochs", type=int, default=40, help="upper epoch limit")
    parser.add_argument(
        "--batch-size", type=int, default=20, metavar="N", help="batch size"
    )
    parser.add_argument("--bptt", type=int, default=35, help="sequence length")
    parser.add_argument(
        "--dropout",
        type=float,
        default=0.2,
        help="dropout applied to layers (0 = no dropout)",
    )
    parser.add_argument(
        "--tied",
        type=bool,
        default=False,
        help="tie the word embedding and softmax weights",
    )
    parser.add_argument("--seed", type=int, default=1111, help="random seed")
    parser.add_argument(
        "--precision", type=str, default="float", help="float | double | half"
    )
    parser.add_argument(
        "--log-interval", type=int, default=200, metavar="N", help="report interval"
    )
    parser.add_argument(
        "--resume-dir", type=str, default=None, help="path to resume training"
    )

    parser.add_argument(f"--{ST_CHECKPOINT_DIR}", type=str)

    args = parser.parse_args()

    download_data(args.input_data_dir)
    input_data_path = Path(args.input_data_dir).joinpath("wikitext-2")
    output_path = Path(args.output_data_dir)

    checkpoint_dir = vars(args)[ST_CHECKPOINT_DIR]

    # Set the random seed manually for reproducibility.
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        if not args.use_cuda:
            print(
                "WARNING: You have a CUDA device, so you should probably run with --use-cuda"
            )

    device = args.device = torch.device("cuda" if args.use_cuda else "cpu")

    # Load data
    corpus = data.Corpus(str(input_data_path))

    eval_batch_size = 10
    train_data = batchify(corpus.train, args.batch_size, device)
    val_data = batchify(corpus.valid, eval_batch_size, device)
    test_data = batchify(corpus.test, eval_batch_size, device)

    # Build the model
    ntokens = len(corpus.dictionary)

    def evaluate(data_source):
        # Turn on evaluation mode which disables dropout.
        model.eval()
        total_loss = 0.0
        ntokens = len(corpus.dictionary)
        with torch.no_grad():
            for i in range(0, data_source.size(0) - 1, args.bptt):
                data, targets = get_batch(data_source, i, args.bptt)
                output = model(data)
                output = output.view(-1, ntokens)
                total_loss += len(data) * criterion(output, targets).item()
        return total_loss / (len(data_source) - 1)

    def train(optimizer, epoch):
        # Turn on training mode which enables dropout.
        model.train()
        total_loss = 0.0
        epoch_loss = 0.0
        start_time = time.time()
        ntokens = len(corpus.dictionary)
        first_loss = None
        for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
            data, targets = get_batch(train_data, i, args.bptt)
            # Starting each batch, we detach the hidden state from how it was previously produced.
            # If we didn't, the model would try backpropagating all the way to start of the dataset.

            optimizer.zero_grad()
            output = model(data)
            output = output.view(-1, ntokens)
            loss = criterion(output, targets)
            if torch.isnan(loss):
                exit(0)
            if args.precision == "half":
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            if args.clip > 0:
                # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
                if args.precision == "half":
                    torch.nn.utils.clip_grad_norm_(
                        amp.master_params(optimizer), args.clip
                    )
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)

            optimizer.step()

            total_loss += loss.item()
            epoch_loss += len(data) * loss.item()

            if batch % args.log_interval == 0 and batch > 0:
                cur_loss = total_loss / args.log_interval
                elapsed = time.time() - start_time
                print(
                    "| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.5f} | ms/batch {:5.2f} | "
                    "loss {:5.2f} | ppl {:8.2f}".format(
                        epoch,
                        batch,
                        len(train_data) // args.bptt,
                        lr,
                        elapsed * 1000 / args.log_interval,
                        cur_loss,
                        np.exp(cur_loss),
                    )
                )
                total_loss = 0
                start_time = time.time()
                if first_loss is None:
                    first_loss = cur_loss

        return epoch_loss / (len(train_data) - 1), first_loss

    model = model.TransformerModel(
        ntokens,
        ninp=args.d_model,
        nhead=args.nhead,
        nhid=args.d_model * args.ffn_ratio,
        nlayers=args.nlayers,
        dropout=args.dropout,
    )

    model = model.to(device)
    model = setprec(model, args.precision)

    criterion = nn.NLLLoss()

    # Loop over epochs.
    lr = args.lr
    best_val_loss = float("inf")

    if args.optimizer_name == "sgd":
        optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)
    elif args.optimizer_name == "adam":
        optimizer = optim.Adam(
            model.parameters(), lr=args.lr, betas=(args.momentum, 0.999)
        )
    else:
        raise ValueError()

    # half-precision black magic
    if args.precision == "half":
        model, optimizer = amp.initialize(
            model, optimizer, opt_level="O1", min_loss_scale=0.0001, verbosity=0
        )

    logs = []
    start_epoch = 0
    if checkpoint_dir:
        if os.path.exists(os.path.join(checkpoint_dir, "checkpoint_last.pt")):
            checkpoint = torch.load(os.path.join(checkpoint_dir, "checkpoint_last.pt"))
            model.load_state_dict(checkpoint["model"])
            optimizer.load_state_dict(checkpoint["optimizer"])
            if args.precision == "half":
                amp.load_state_dict(checkpoint["amp"])
            start_epoch = checkpoint["epoch"]
            best_val_loss = checkpoint["best_val_loss"]
            logs = checkpoint["logs"]
        else:
            os.makedirs(checkpoint_dir)

    # At any point you can hit Ctrl + C to break out of training early.
    try:
        for epoch in range(start_epoch + 1, args.epochs + 1):
            epoch_start_time = time.time()
            train_loss, first_loss = train(optimizer, epoch)
            val_loss = evaluate(val_data)

            duration = time.time() - epoch_start_time
            print("-" * 89)
            print(
                "| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | "
                "valid ppl {:8.2f}".format(epoch, duration, val_loss, np.exp(val_loss))
            )
            print("-" * 89)
            logs.append(
                dict(
                    epoch=epoch,
                    train_loss=train_loss,
                    val_loss=val_loss,
                    first_loss=first_loss,
                    duration=duration,
                )
            )

            # report validation loss back to Syne Tune
            report(epoch=epoch, val_loss=val_loss)

            # Save the model if the validation loss is the best we've seen so far.
            if checkpoint_dir is not None:
                if val_loss < best_val_loss:
                    checkpoint = {
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "epoch": epoch,
                        "best_val_loss": best_val_loss,
                        "logs": logs,
                    }
                    if args.precision == "half":
                        checkpoint["amp"] = (amp.state_dict(),)
                    with open(
                        os.path.join(checkpoint_dir, "checkpoint_best.pt"), "wb"
                    ) as f:
                        torch.save(checkpoint, f)
                    best_val_loss = val_loss
                else:
                    checkpoint = {
                        "model": model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "epoch": epoch,
                        "best_val_loss": best_val_loss,
                        "logs": logs,
                    }
                    if args.precision == "half":
                        checkpoint["amp"] = amp.state_dict()
                with open(
                    os.path.join(checkpoint_dir, "checkpoint_last.pt"), "wb"
                ) as f:
                    torch.save(checkpoint, f)

    except KeyboardInterrupt:
        print("-" * 89)
        print("Exiting from training early")
import argparse
import json
import time
import os

import jax
import numpy as np
import optax

import wandb
from tqdm import tqdm


from mesh_transformer import util
from mesh_transformer.checkpoint import read_ckpt, write_ckpt
from mesh_transformer.transformer_shard import CausalTransformer
from tfrecord_loader import TFRecordNewInputs
from smart_open import open
from google.cloud import storage
from google.cloud.exceptions import NotFound

from mesh_transformer.util import clip_by_global_norm, additive_weight_decay


def parse_args():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="""
    To use, download the full checkpoint archive, extract and upload to a GCS bucket, and set that as --tune-model-path
    Modify the config file:
        - set `tpu_size` to 8 (if on a v3-8)
        - set `warmup_steps`, `anneal_steps`, `lr`, `end_lr` to the lr schedule for your finetuning run
        - the global step will reset to 0, keep that in mind when writing your lr schedule
    To prepare data in the expected data format:
        - use this notebook: https://github.com/EleutherAI/gpt-neo/blob/master/GPTNeo_example_notebook.ipynb
        - after creating .tfrecords files, save their paths to a index file under `data/`, see existing files for examples
    """,
    formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--config", type=str, default=None, help="Config file location")
    parser.add_argument("--model-dir", type=str, default=None, help="Output directory")
    parser.add_argument("--train-set", type=str, default=None, help="Training files location")
    parser.add_argument("--tune-model-path", type=str, default=None, help="Base model to finetune")
    parser.add_argument("--fresh-opt", default=False, action="store_true", help="Use a newly initialized optimizer, ignoring any optimizer state saved in the base checkpoint")

    args = parser.parse_args()
    return args


def save(network, step, path, mp, aux=None, keep_n=2, delete_old=True):
    assert path
    client = storage.Client()

    if not path.startswith("gs://"):
        try:
            os.makedirs(f"{path}/step_{step}")
        except:
            pass

    if aux is None:
        aux = {}

    try:
        with open(f"{path}/meta.json", "r") as f:
            meta = json.load(f)
    except:
        # create metadata file
        with open(f"{path}/meta.json", "w") as f:
            json.dump({
                "step": 0,
                "checkpoints": [],
                "aux": {}
            }, f)

    # do sharded checkpoint writing
    start = time.time()
    res = []
    for shard_id in range(mp):
        write_ckpt(network.state, f"{path}/step_{step}/", shard_id)

    print(f"Wrote checkpoint in {time.time() - start:.06}s")

    with open(f"{path}/meta.json", "r") as f:
        meta = json.load(f)

    meta["step"] = step
    meta["checkpoints"].append(step)
    all_aux = meta.get("aux", {})

    while len(meta["checkpoints"]) > keep_n:
        ckpt_to_delete = meta["checkpoints"].pop(0)

        try:
            del all_aux[str(ckpt_to_delete)]
        except:
            print(f"failed to delete the aux state for {step}")

        if delete_old:
            print(f"deleting checkpoint {ckpt_to_delete}")
            os.remove(f"{path}/step_{ckpt_to_delete}/")
        else:
            print(f"keeping checkpoint {ckpt_to_delete}")

    all_aux[step] = aux
    meta["aux"] = all_aux

    with open(f"{path}/meta.json", "w") as f:
        json.dump(meta, f)


def train_step(network, data):
    inputs = {
        "obs": data[:, :, :-1],
        "target": data[:, :, 1:],
    }

    loss, last_loss = network.train(inputs)

    return np.array(loss).mean(), np.array(last_loss).mean()


def eval_step(network, data):
    inputs = {
        "obs": data[:, :-1],
        "target": data[:, 1:],
    }

    out = network.eval(inputs)
    loss = out["loss"]

    return np.array(loss).mean()


if __name__ == "__main__":
    args = parse_args()
    params = json.load(open(args.config))

    gradient_accumulation_steps = params.get("gradient_accumulation_steps", 1)
    per_replica_batch = params["per_replica_batch"]
    cores_per_replica = params["cores_per_replica"]

    assert cores_per_replica <= 8

    model_dir = args.model_dir
    layers = params["layers"]
    d_model = params["d_model"]
    n_heads = params["n_heads"]
    n_vocab = params["n_vocab"]
    seq = params["seq"]
    norm = params["norm"]

    val_batches = params["val_batches"]
    val_every = params["val_every"]
    ckpt_every = params["ckpt_every"]
    total_steps = params["total_steps"]

    pe = params["pe"]
    assert pe in ["fixed", "rotary", "t5"]

    warmup_steps = params["warmup_steps"]
    anneal_steps = params["anneal_steps"]
    lr = params["lr"]
    end_lr = params["end_lr"]
    weight_decay = params["weight_decay"]

    opt = optax.chain(
        optax.scale(1 / gradient_accumulation_steps),
        clip_by_global_norm(1),
        optax.scale_by_adam(),
        additive_weight_decay(weight_decay),
        optax.scale(-1),
        optax.scale_by_schedule(util.gpt3_schedule(warmup_steps, anneal_steps, lr, end_lr))
    )

    params["optimizer"] = opt

    start = time.time()
    tpu_size = jax.device_count()
    if tpu_size < cores_per_replica:
        msg = f"each shard needs a separate device, but device count ({tpu_size}) < shard count ({cores_per_replica})"
        raise ValueError(msg)
    print(f"jax devices: {tpu_size}")
    print(f"jax runtime initialized in {time.time() - start:.06}s")

    mesh_shape = (tpu_size // cores_per_replica, cores_per_replica)
    devices = np.array(jax.devices()).reshape(mesh_shape)

    # pick initial ckpt - based on tuning vs train from scratch

    step = 0
    initial_ckpt_state_path = None
    train_loader = None

    if args.tune_model_path:
        print('`--tune_model_path` passed: we are beginning a fine-tuning run')
        fine_tuning = True
        initial_ckpt_state_path = args.tune_model_path
    else:
        print('`--tune_model_path` not passed: we are continuing a fine-tuning run from a checkpoint (or we are not fine-tuning)')
        fine_tuning = False
        initial_ckpt_model_dir = model_dir
        initial_ckpt_path = f"{initial_ckpt_model_dir}"
        meta_path = f"{initial_ckpt_path}/meta.json"

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            ckpt_step = meta["checkpoints"][-1]
            initial_ckpt_state_path = f"{initial_ckpt_path}/step_{ckpt_step}/"
            print(f"state will be restored from checkpoint {ckpt_step}")

            step = ckpt_step
            train_loader = meta['aux'][str(ckpt_step)].get("train_loader", None)
        except NotFound:
            # no checkpoint, start at zero
            print(f"No checkpoint to load at {initial_ckpt_path}. Training from scratch.")

    if initial_ckpt_state_path:
        print(f"path to load checkpoint from: {initial_ckpt_state_path}")
    else:
        print("not loading from a checkpoint")

    # set up datasets
    print("setting up datasets")

    train_dataset = TFRecordNewInputs(f"{args.train_set}",
                                      batch_size=(
                                          gradient_accumulation_steps,
                                          per_replica_batch * tpu_size // cores_per_replica),
                                      sample_size=params['seq'],
                                      restore_state=train_loader)

    global_val_batch = per_replica_batch * tpu_size // cores_per_replica

    val_sets = {}

    for k, v in params['val_set'].items():
        val_sets[k] = TFRecordNewInputs(f"data/{v}",
                                        batch_size=(global_val_batch,),
                                        sample_size=seq)

    # tok/sec metrics
    windows_per_step = gradient_accumulation_steps * (per_replica_batch * tpu_size // cores_per_replica)
    tokens_per_step = params['seq'] * windows_per_step

    # load + run
    with jax.experimental.maps.mesh(devices, ('dp', 'mp')):
        print("initializing network")
        network = CausalTransformer(params)

        if initial_ckpt_state_path:
            print("loading network")
            if fine_tuning:
                # get the scheduler step stored in the just-initialized optimizer
                # should be zero
                init_sched_state = network.state["opt_state"][-1]

            start = time.time()
            # TODO (mitchg) shouldn't this detect the number of input shards automatically?
            network.state = read_ckpt(network.state, initial_ckpt_state_path, devices.shape[1], load_opt=(not args.fresh_opt))

            if fine_tuning:
                # overwrite the loaded scheduler step with zeros
                # this makes fine-tuning use the lr schedule in
                network.state["opt_state"][-1] = init_sched_state

            print(f"network loaded in {time.time() - start:.06}s")

        print('compiling train fn')
        start = time.time()
        train_step(network, train_dataset.get_samples())
        step += 1
        print(f"Train fn compiled in {time.time() - start:.06}s")

        print('compiling eval fn')
        start = time.time()
        for val_set in val_sets.values():
            eval_step(network, val_set.get_samples())
            val_set.reset()
        print(f"Eval fn compiled in {time.time() - start:.06}s")

        wandb.init(project='mesh-transformer-jax', name=params["name"], config=params)

        while True:
            if (step % ckpt_every == 1) or step == total_steps:
                print(f"saving a checkpoint for step {step}")
                save(network, step, model_dir,
                     mp=cores_per_replica,
                     aux={"train_loader": train_dataset.get_state()},
                     delete_old=True,
                     )

            if step % val_every == 1:  # 1 because we've already taken a step to compile train fn
                for name, val_set in val_sets.items():
                    val_loss = []
                    for i, _ in tqdm(zip(val_set.sample_once(), range(val_batches)),
                                     desc=f"validation for step {step}, set {name}",
                                     total=val_batches):
                        val_loss.append(eval_step(network, i))
                    val_set.reset()

                    val_loss = np.array(val_loss).mean()
                    print(f"validation loss for step {step}, set {name}: {val_loss}")

                    wandb.log({f'val/loss_{name}': float(val_loss)}, step)

            if step == total_steps:
                print("training completed!")
                exit()

            start = time.time()
            loss, last_loss = train_step(network, train_dataset.get_samples())
            step += 1

            steps_per_sec = 1 / (time.time() - start)
            tokens_per_sec = tokens_per_step * steps_per_sec

            wandb.log({'train/loss': loss, 'train/last_loss': last_loss, 'train/steps_per_sec': steps_per_sec, 'train/tokens_per_sec': tokens_per_sec}, step)

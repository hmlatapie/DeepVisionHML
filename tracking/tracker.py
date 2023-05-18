# Copyright 2022 Cisco Systems, Inc. and its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

import json
import mmcv
import numpy as np
import pickle
import redis
from argparse import ArgumentParser
from redis import Redis
from urllib.parse import urlparse

from Monitor import GPUCalculator, MMTMonitor
from mmtracking.mmtrack.apis import inference_mot, init_model
from outside_track import outside_tracker_manager as om

redis_client = redis.StrictRedis("redistimeseries", 6379)

# GPUCalculator and MMTMonitor variables
model_run_latency = MMTMonitor(redis_client, "model_run_latency")
bounding_boxes_latency = MMTMonitor(redis_client, "bounding_boxes_latency")
gpu_calculation = GPUCalculator(redis_client)


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)


def results2outs(bbox_results = None,
                 mask_results = None,
                 mask_shape = None,
                 **kwargs):
    outputs = dict()

    if bbox_results is not None:
        labels = []
        for i, bbox in enumerate(bbox_results):
            labels.extend([i] * bbox.shape[0])
        labels = np.array(labels, dtype=np.int64)
        outputs["labels"] = labels

        bboxes = np.concatenate(bbox_results, axis=0).astype(np.float32)
        if bboxes.shape[1] == 5:
            outputs["bboxes"] = bboxes
        elif bboxes.shape[1] == 6:
            ids = bboxes[:, 0].astype(np.int64)
            bboxes = bboxes[:, 1:]
            outputs["bboxes"] = bboxes
            outputs["ids"] = ids
        else:
            raise NotImplementedError(
                f"Not supported bbox shape: (N, {bboxes.shape[1]})")

    if mask_results is not None:
        assert mask_shape is not None
        mask_height, mask_width = mask_shape
        mask_results = mmcv.concat_list(mask_results)
        if len(mask_results) == 0:
            masks = np.zeros((0, mask_height, mask_width)).astype(bool)
        else:
            masks = np.stack(mask_results, axis=0)
        outputs["masks"] = masks

    return outputs


def outs_tracks_compare(original_tracks, new_tracks):
    """
    Check whether the new_track is consistent (with some differences) with the original track.

    The new tracks should include the original tracks.
    """
    count = 0
    thresholding = 20
    for bbox_original in original_tracks.get("bboxes", None)[:, :4]:
        for bbox_new in new_tracks.get("bboxes", None)[:, :4]:
            if abs(bbox_new[0] - bbox_original[0]) < thresholding:
                if abs(bbox_new[1] - bbox_original[1]) < thresholding:
                    if abs(bbox_new[2] - bbox_original[2]) < thresholding:
                        if abs(bbox_new[3] - bbox_original[3]) < thresholding:
                            count += 1
                            break

    return count / original_tracks.get("bboxes", None).shape[0]


def main():
    parser = ArgumentParser()
    parser.add_argument("config", help="config file")
    parser.add_argument("--input_stream", help="input stream key for coming frames", type=str, default="camera:0")
    parser.add_argument("--classId", help="class category of the objects", type=str, default="PERSON")
    parser.add_argument("--output_stream", help="output stream key for tracklets", type=str, default="camera:0:mot")
    parser.add_argument("--checkpoint", help="checkpoint file")
    parser.add_argument("--device", default="cuda:0", help="device used for inference")
    parser.add_argument("--redis", help="Redis URL", type=str, default="redis://127.0.0.1:6379")
    parser.add_argument("--maxlen", help="Maximum length of output stream", type=int, default=3000)
    parser.add_argument("--additional_model",
                        help="to run multiple models, here is the path of the config file of the additional model")

    args = parser.parse_args()

    url = urlparse(args.redis)
    conn = Redis(host=url.hostname, port=url.port, health_check_interval=25)
    if not conn.ping():
        raise Exception("Redis unavailable")

    last_id = 0
    model = init_model(args.config, args.checkpoint, device=args.device)

    # TODO, naming issue

    # introducing additional model, TODO, don't know how to use it
    additional_model = init_model(args.additional_model, args.checkpoint, device=args.device)

    outside_tracker_manager = om()

    while True:
        try:
            resp = conn.xread({args.input_stream: last_id}, count=1)

            if resp:
                key, messages = resp[0]
                ref_id, data = messages[0]
                if data:
                    frameId = int(data.get(b"frameId").decode())

                    if frameId == 0:
                        outside_tracker_manager.reset()

                    print("num outside tracks: ",
                          sum([1 if each is not None else 0 for each in outside_tracker_manager.internal_tracks]))

                    print("Frame ID: ", frameId)
                    img = pickle.loads(data[b"image"])

                    redis_client.execute_command("ts.add framerate * {}".format(frameId))
                    model_run_latency.start_timer()
                    result = inference_mot(model, img, frame_id=frameId)
                    model_run_latency.end_timer()
                    bounding_boxes_latency.start_timer()
                    original_tracks = results2outs(bbox_results=result.get("track_bboxes", None))

                    # additional model(s) processing
                    additional_result = inference_mot(additional_model, img, frame_id=frameId)
                    additional_tracks = results2outs(bbox_results=additional_result.get("track_bboxes", None))

                    new_tracks = outside_tracker_manager.step(original_tracks)

                    # switched from original and enhanced tracks
                    outs_tracks = new_tracks

                    bboxes = outs_tracks.get("bboxes", None)
                    bounding_boxes_latency.end_timer()
                    gpu_calculation.add()
                    ids = outs_tracks.get("ids", None)
                    objects_list = []
                    for (i, id) in enumerate(ids):
                        object_dict = {"objectId": id, "object_bbox": bboxes[i], "class": args.classId}
                        objects_list.append(object_dict)
                    frame_dict = {"frameId": frameId, "tracking_info": objects_list}
                    conn.xadd(args.output_stream,
                              {"refId": last_id, "tracking": json.dumps(frame_dict, cls=NpEncoder)}, maxlen=args.maxlen)
                    last_id = ref_id
        except ConnectionError as e:
            print("ERROR REDIS CONNECTION: {}".format(e))


if __name__ == "__main__":
    main()

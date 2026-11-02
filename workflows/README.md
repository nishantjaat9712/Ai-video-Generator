# Connecting a real ComfyUI video workflow

The adapter sends an exported **API-format graph** to a running ComfyUI server. It does not install models, invent nodes, download weights, or turn an image slideshow into a generated clip.

1. Install ComfyUI from [the official Windows guide](https://docs.comfy.org/installation/desktop/windows) into a D-drive location. Keep model downloads and caches on D:.
2. Follow an [official Wan video workflow](https://docs.comfy.org/tutorials/video/wan/wan2_2). Install its exact weights and nodes and read its model license.
3. Run the workflow successfully in ComfyUI itself. The detected RTX 3050 Laptop has 4 GB VRAM; a standard Wan 2.2 TI2V-5B reference configuration requires much more memory. A remote GPU running ComfyUI is an alternative. This app does not validate community low-memory claims.
4. Export the graph in API format. Save it under this project's `workflows` directory. It must contain a video save/combine node that exposes an MP4, WebM or MOV through ComfyUI history.
5. Wrap the graph in a manifest using the helper below. Inspect your graph to find its actual node IDs and input names; the example IDs below are illustrative only.
6. Enter the manifest's absolute D: path and your ComfyUI server URL in Settings. HTTP is allowed for loopback only; remote servers need HTTPS. The default local server is `http://127.0.0.1:8188`.
7. Start with one short scene. A reachable server does not prove its model files, VRAM capacity or output nodes are correct. Verify one real generation before a long story.

Example mapping command (replace every binding with a real input from your graph):

```powershell
.\.venv\Scripts\python.exe .\scripts\Bind-ComfyWorkflow.py --workflow .\workflows\exported-api.json --output .\workflows\wan-manifest.json --prompt 6:text --width 5:width --height 5:height --frames 5:length --fps 9:frame_rate --seed 3:seed --fps-value 24 --resolution 720p --ratio 9:16 --width-value 704 --height-value 1280
```

Add `--image NODE:INPUT` for an image input. The helper checks bindings exist; it cannot prove the selected model supports the declared modes, resolution or frame count. Declare only settings you tested. It defaults to five-second clips. Multiple saved clips can be assembled into a longer video.

The JSON manifest contains:

- `workflow`: the exported node graph.
- `inputs`: mappings such as `"prompt": ["6", "text"]`, plus width, height, num_frames, fps and seed; image is optional.
- `capabilities`: modes, resolutions, aspect_ratios, durations and fps arrays.
- `dimensions`: optional model-specific dimensions, e.g. 704×1280 for Wan 2.2 TI2V-5B portrait. These need not be exactly 720 pixels on the short edge.
- The adapter uses `duration * fps + 1` frames. Confirm this matches your workflow's valid frame-count rule.

Only one local inference job is intended to use the GPU at once. ComfyUI owns GPU allocations; configure its own model offload/low-VRAM options as appropriate. The API worker does not pretend that CPU memory cleanup releases another process's GPU memory.

Cancellation removes pending requests. Running requests on a shared ComfyUI server are allowed to finish, because sending a global interrupt could cancel someone else's job. The studio stops subsequent pipeline stages. Actual latency, motion quality and character continuity depend on the model and workflow.


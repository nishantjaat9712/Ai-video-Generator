"""Bind a user-tested ComfyUI API graph. Does not download models or run inference."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--workflow",required=True)
parser.add_argument("--output",required=True)
for field in ("prompt","width","height","frames","fps","seed"):
    parser.add_argument("--"+field,required=True,help="Existing node:input binding")
parser.add_argument("--image")
parser.add_argument("--fps-value",type=int,choices=[16,24,25,30],default=24)
parser.add_argument("--resolution",choices=["480p","720p"],default="720p")
parser.add_argument("--ratio",choices=["9:16","16:9","1:1"],default="9:16")
parser.add_argument("--duration",type=int,choices=[5,10],default=5)
parser.add_argument("--width-value",type=int,required=True)
parser.add_argument("--height-value",type=int,required=True)
args=parser.parse_args()
source=Path(args.workflow).resolve()
target=Path(args.output).resolve()
for path in (source,target):
    try:path.relative_to(ROOT)
    except ValueError:parser.error("Workflow and manifest must be inside this project on D:.")
if target.exists():parser.error("Output already exists; choose a new filename.")
if source.stat().st_size>5*1024*1024:parser.error("Graph exceeds 5 MB.")
graph=json.loads(source.read_text(encoding="utf-8-sig"))
if not isinstance(graph,dict) or "nodes" in graph:
    parser.error("Use API-format graph JSON, not a UI workflow export.")
bindings={}
for field in ("prompt","width","height","frames","fps","seed","image"):
    value=getattr(args,field)
    if not value:continue
    try:node,input_name=value.split(":",1)
    except ValueError:parser.error("Bindings use NODE:INPUT format.")
    if node not in graph or input_name not in graph[node].get("inputs",{}):
        parser.error(f"Binding {value} is not present in the graph.")
    bindings["num_frames" if field=="frames" else field]=[node,input_name]
if min(args.width_value,args.height_value)<64 or max(args.width_value,args.height_value)>4096:
    parser.error("Dimensions must be between 64 and 4096.")
doc={"workflow":graph,"inputs":bindings,"capabilities":{"modes":["image"] if args.image else ["text"],
    "resolutions":[args.resolution],"aspect_ratios":[args.ratio],"durations":[args.duration],"fps":[args.fps_value]},
    "dimensions":{args.resolution:{args.ratio:[args.width_value,args.height_value]}}}
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(doc,indent=2),encoding="utf-8")
print(f"Saved {target}. Bindings checked; real model capabilities still require a successful ComfyUI run.")


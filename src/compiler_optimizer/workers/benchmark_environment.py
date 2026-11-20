"""Trusted metadata probe, run under the same sandbox policy as measurements."""

PROBE = r"""
import base64,hashlib,json,os,resource,sys,time
from pathlib import Path
started=time.perf_counter()
allowed=sorted(os.sched_getaffinity(0))
requested=None if sys.argv[1]=="auto" else int(sys.argv[1])
cpu=allowed[0] if requested is None else requested
if cpu not in allowed: raise ValueError("CPU is not available to this worker")
text=Path("/proc/cpuinfo").read_text()[:1024*1024]
blocks=text.split("\n\n")
selected={}
for block in blocks:
    fields=dict(line.split(":",1) for line in block.splitlines() if ":" in line)
    fields={k.strip():v.strip() for k,v in fields.items()}
    if fields.get("processor")==str(cpu): selected=fields; break
model=(selected.get("model name") or selected.get("Hardware")
       or selected.get("Processor") or os.uname().machine)
features=selected.get("flags",selected.get("Features",""))
governor=Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
try: governor=governor.read_text().strip()
except OSError: governor=None
data={"boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
      "cpu_id":cpu,"allowed_cpus":allowed,"cpu_model":model[:512],
      "cpu_features_sha256":hashlib.sha256(features.encode()).hexdigest(),
      "kernel":os.uname().release,"governor":governor,
      "clock_resolution_seconds":time.get_clock_info("perf_counter").resolution,
      "load_average":list(os.getloadavg())}
print(json.dumps({"stdout":base64.b64encode(json.dumps(data).encode()).decode(),"stderr":"",
      "wall_seconds":time.perf_counter()-started,
      "peak_memory_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
      "returncode":0,"failure":None,"policy_version":POLICY_VERSION,
      "policy_digest":POLICY_DIGEST,"build_profile":"normal"}))
"""

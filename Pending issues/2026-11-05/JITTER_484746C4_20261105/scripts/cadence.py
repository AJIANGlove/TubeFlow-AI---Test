"""Held-frame cadence: share of moving 12-frame windows whose motion happens only every other frame."""
import sys, subprocess, json, numpy as np
def cadence(path):
    W,H=264,152
    raw=subprocess.run(["ffmpeg","-v","error","-i",path,"-vf",f"scale={W}:{H},format=gray","-f","rawvideo","-"],capture_output=True).stdout
    F=np.frombuffer(raw,np.uint8).reshape(-1,H,W).astype(np.float32)
    d=np.abs(np.diff(F,axis=0)).mean((1,2))
    med=np.median(d); cuts=set(np.where(d>max(12,6*med))[0])
    moving=held=0; spans=[]
    for s in range(0,len(d)-12,6):
        w=d[s:s+12]
        if any(i in cuts for i in range(s,s+12)) or w.mean()<0.8: continue
        moving+=1
        a,b=w[0::2].mean(),w[1::2].mean()
        if min(a,b)/max(a,b)<0.35: held+=1; spans.append(round(s/24,1))
    return {"moving_windows":moving,"held_windows":held,"held_share":round(held/moving,2) if moving else None,"held_at_s":spans[:12]}
for p in sys.argv[1:]:
    print(p.split('/')[-1][:12], json.dumps(cadence(p)))

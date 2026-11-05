"""Per-frame motion analysis of a video: global shift (phase correlation), shot cuts,
HF jitter (shift minus 5-frame moving average, RMS) per shot, residual flicker after alignment,
and per-quadrant local shift (to tell camera shake from local wobble)."""
import json, subprocess, sys
import numpy as np, cv2
path = sys.argv[1]
W, H = 528, 304
raw = subprocess.run(["ffmpeg","-v","error","-i",path,"-vf",f"scale={W}:{H},format=gray","-f","rawvideo","-"],capture_output=True).stdout
frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)
n = len(frames)
win = cv2.createHanningWindow((W, H), cv2.CV_32F)
def shift(a, b, region=None):
    if region:
        y0,y1,x0,x1 = region; a=a[y0:y1,x0:x1]; b=b[y0:y1,x0:x1]
        w = cv2.createHanningWindow((a.shape[1],a.shape[0]), cv2.CV_32F)
    else: w = win
    (dx, dy), resp = cv2.phaseCorrelate(a, b, w)
    return dx, dy, resp
dxy=[]; cut=[]; resid=[]; quads=[]
Q=[(0,H//2,0,W//2),(0,H//2,W//2,W),(H//2,H,0,W//2),(H//2,H,W//2,W)]
for i in range(1,n):
    a,b=frames[i-1],frames[i]
    dx,dy,r=shift(a,b)
    diff=np.abs(a-b).mean()
    dxy.append((dx,dy)); cut.append(diff)
    M=np.float32([[1,0,-dx],[0,1,-dy]])
    bb=cv2.warpAffine(b,M,(W,H),borderMode=cv2.BORDER_REPLICATE)
    resid.append(float(np.abs(a[8:-8,8:-8]-bb[8:-8,8:-8]).mean()))
    quads.append([shift(a,b,q)[:2] for q in Q])
dxy=np.array(dxy); cut=np.array(cut); resid=np.array(resid); quads=np.array(quads)
# shot cuts: frame diff spikes
med=np.median(cut); cuts=[i+1 for i in range(len(cut)) if cut[i]>max(12, 6*med)]
bounds=[0]+cuts+[n]
shots=[]
for s,e in zip(bounds[:-1],bounds[1:]):
    seg=dxy[s:e-1] if e-1>s else dxy[s:s+1]
    if len(seg)<6: continue
    k=5; pad=np.pad(seg,((k//2,k//2),(0,0)),mode='edge')
    ma=np.array([pad[i:i+k].mean(0) for i in range(len(seg))])
    res=seg-ma
    hf=float(np.sqrt((res**2).sum(1).mean()))
    spd=float(np.sqrt((ma**2).sum(1)).mean())
    q=quads[s:e-1]
    # local wobble: disagreement between quadrant shifts
    qd=float(np.sqrt(((q-q.mean(1,keepdims=True))**2).sum(2)).mean())
    shots.append({"start":s,"end":e,"t0":round(s/24,2),"t1":round(e/24,2),"hf":round(hf,3),
                  "mean_speed":round(spd,3),"drift_px":[round(float(seg[:,0].sum()),1),round(float(seg[:,1].sum()),1)],
                  "resid_mean":round(float(resid[s:e-1].mean()),2),"resid_p95":round(float(np.percentile(resid[s:e-1],95)),2),
                  "quad_disagree":round(qd,3)})
print(json.dumps({"frames":n,"cuts":cuts,"shots":shots}))
np.save(path.split('/')[-1][:8]+"_dxy.npy", np.c_[dxy, resid, cut])

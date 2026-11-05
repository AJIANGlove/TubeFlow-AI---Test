import sys, subprocess, numpy as np, cv2, json
path=sys.argv[1]; W,H=1056,608
raw=subprocess.run(["ffmpeg","-v","error","-i",path,"-vf","format=gray","-f","rawvideo","-"],capture_output=True).stdout
F=np.frombuffer(raw,np.uint8).reshape(-1,H,W)
rows=[]
for i in range(len(F)-1):
    a,b=F[i],F[i+1]
    p=cv2.goodFeaturesToTrack(a,800,0.01,8)
    if p is None or len(p)<20: rows.append((0,0,1,0,0,0)); continue
    q,st,_=cv2.calcOpticalFlowPyrLK(a,b,p,None,winSize=(21,21),maxLevel=3)
    ok=st.ravel()==1; p,q=p[ok],q[ok]
    M,inl=cv2.estimateAffinePartial2D(p,q,method=cv2.RANSAC,ransacReprojThreshold=1.0)
    if M is None: rows.append((0,0,1,0,0,0)); continue
    s=float(np.hypot(M[0,0],M[1,0])); r=float(np.degrees(np.arctan2(M[1,0],M[0,0])))
    inl=inl.ravel().astype(bool)
    resid=np.linalg.norm((q-(p@M[:,:2].T+M[:,2])).reshape(-1,2),axis=1)
    out_mag=float(np.median(resid[~inl])) if (~inl).sum()>5 else 0.0
    rows.append((float(M[0,2]),float(M[1,2]),s,r,float(inl.mean()),out_mag))
np.save(path.split('/')[-1][:8]+"_aff.npy",np.array(rows))

import numpy as np
from capagent.denoise import TemporalMedianDenoiser

FX = FY = 361.376; CX, CY = 318.517, 179.502
ZMIN, ZMAX = 200, 4000

def fit_plane_rms(depth):
    mask = (depth > ZMIN) & (depth < ZMAX)
    ys, xs = np.nonzero(mask)
    if len(ys) < 200:
        return None
    z = depth[ys, xs].astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    pts = np.stack([x, y, z], axis=1)
    c = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    n = vt[-1]
    if n[2] > 0:
        n = -n
    d = float(n @ c)
    res = pts @ n - d
    sigma = np.median(np.abs(res)) / 0.6745
    inl = np.abs(res) < 3.0 * sigma
    if inl.sum() < 50:
        return None
    rms_sq = res[inl] ** 2
    rms_val = np.sqrt(np.mean(rms_sq))
    return float(rms_val)

# Check all wall v3b sequences
for seq in range(4):
    path = f'D:\\项目\\交互艺术\\展厅孪生\\ai\\hp60c_audit\\audit_output\\cap_lr_wall_v3b\\seq_00000{seq}.npz'
    data = np.load(path)
    depths = data['depth']
    
    denoiser = TemporalMedianDenoiser(window_size=5, depth_shape=(360, 640))
    denoised = []
    for i in range(len(depths)):
        out = denoiser.process(depths[i].tobytes())
        denoised.append(np.frombuffer(out, dtype=np.uint16).reshape((360, 640)))
    denoised = np.array(denoised)
    
    # Compute RMS for frames after window is full
    raw_rms = []
    den_rms = []
    for i in range(5, len(depths)):
        r_raw = fit_plane_rms(depths[i])
        r_den = fit_plane_rms(denoised[i])
        if r_raw and r_den:
            raw_rms.append(r_raw)
            den_rms.append(r_den)
    
    if raw_rms:
        print(f'seq_{seq}: {len(depths)} frames')
        print(f'  Raw:    p50={np.median(raw_rms):.1f} p75={np.percentile(raw_rms, 75):.1f} p95={np.percentile(raw_rms, 95):.1f}')
        print(f'  Denoised: p50={np.median(den_rms):.1f} p75={np.percentile(den_rms, 75):.1f} p95={np.percentile(den_rms, 95):.1f}')
        print(f'  Reduction: {(np.median(raw_rms)-np.median(den_rms))/np.median(raw_rms)*100:.1f}%')
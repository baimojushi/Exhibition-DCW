import numpy as np
from capagent.denoise import TemporalMedianDenoiser

data = np.load(r'D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\cap_lr_wall_v3b\seq_000000.npz')
depths = data['depth']
denoiser = TemporalMedianDenoiser(window_size=5, depth_shape=(360, 640))
denoised = []
for i in range(len(depths)):
    out = denoiser.process(depths[i].tobytes())
    denoised.append(np.frombuffer(out, dtype=np.uint16).reshape((360, 640)))
denoised = np.array(denoised)

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

for i in [100, 110, 120, 130, 140]:
    rms_raw = fit_plane_rms(depths[i])
    rms_den = fit_plane_rms(denoised[i])
    print(f'Frame {i}: raw={rms_raw:.1f}mm, denoised={rms_den:.1f}mm')
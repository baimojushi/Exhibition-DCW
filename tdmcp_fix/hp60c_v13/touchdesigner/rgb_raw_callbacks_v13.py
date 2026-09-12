# v13 RGB cache -> Script TOP adapter.
import numpy as np

BLACK = np.zeros(
    (480, 640, 4),
    dtype=np.float32,
)


def onCook(scriptOp):
    try:
        rgb = scriptOp.parent().fetch(
            'rgb_frame_u8',
            None,
        )

        if rgb is None:
            scriptOp.copyNumpyArray(BLACK)
            return

        rgb = np.asarray(rgb)

        if rgb.shape != (480, 640, 3):
            raise ValueError(
                'RGB cache shape {}'.format(
                    rgb.shape
                )
            )

        rgb_f = (
            rgb.astype(np.float32)
            * (1.0 / 255.0)
        )

        rgba = np.empty(
            (480, 640, 4),
            dtype=np.float32,
        )
        rgba[:, :, :3] = rgb_f
        rgba[:, :, 3] = 1.0

        scriptOp.copyNumpyArray(
            np.ascontiguousarray(rgba)
        )

        scriptOp.store(
            'frame_meta',
            scriptOp.parent().fetch(
                'rgb_frame_meta',
                {},
            ),
        )
        scriptOp.store(
            'last_error',
            '',
        )

    except Exception as exc:
        scriptOp.store(
            'last_error',
            '{}: {}'.format(
                type(exc).__name__,
                exc,
            ),
        )
        scriptOp.copyNumpyArray(BLACK)

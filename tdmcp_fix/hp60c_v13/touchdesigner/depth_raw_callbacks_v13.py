# v13 Depth cache -> Script TOP adapter.
import numpy as np

ZERO = np.zeros(
    (480, 640, 1),
    dtype=np.float32,
)


def onCook(scriptOp):
    try:
        depth = scriptOp.parent().fetch(
            'depth_mm_u16',
            None,
        )

        if depth is None:
            scriptOp.copyNumpyArray(ZERO)
            return

        depth = np.asarray(depth)

        if depth.shape != (480, 640):
            raise ValueError(
                'Depth cache shape {}'.format(
                    depth.shape
                )
            )

        top = np.ascontiguousarray(
            (
                depth.astype(np.float32)
                * 0.001
            )[:, :, None]
        )

        scriptOp.copyNumpyArray(top)

        scriptOp.store(
            'frame_meta',
            scriptOp.parent().fetch(
                'depth_frame_meta',
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
        scriptOp.copyNumpyArray(ZERO)

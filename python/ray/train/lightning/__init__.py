# isort: off
try:
    import pytorch_lightning  # noqa: F401
except ModuleNotFoundError:
    raise ModuleNotFoundError(
        "PyTorch Lightning isn't installed. To install PyTorch, run 'pip install pytorch_lightning'"
    )
# isort: on

__all__ = []

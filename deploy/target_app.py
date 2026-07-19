"""Compatibility shim to import deploy/target-app/main.py (folder name contains '-')
Expose `app` variable for uvicorn import: `deploy.target_app:app`
"""
import importlib.util
import os

SRC = os.path.join(os.path.dirname(__file__), 'target-app', 'main.py')
spec = importlib.util.spec_from_file_location('deploy.target_app_main', SRC)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Copy the FastAPI app object for uvicorn
app = getattr(module, 'app')

# Optional: expose helper references if needed
__all__ = ['app']

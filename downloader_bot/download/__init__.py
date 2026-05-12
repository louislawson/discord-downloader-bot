"""Worker-side download pipeline.

Pure async modules with no Taskiq dependency. The Taskiq task in
`app/tasks/download.py` orchestrates calls into here.
"""

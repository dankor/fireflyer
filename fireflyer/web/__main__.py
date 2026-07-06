import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "fireflyer.web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        # Templates and CSS are read once at import, so a .css/.html edit only
        # takes effect after the module re-imports. Watch those extensions too
        # (uvicorn defaults to *.py only) so saving any of them reloads the
        # worker and the next page load serves fresh assets.
        reload_includes=["*.py", "*.css", "*.html"],
    )

from stelvio.app import StelvioApp

app = StelvioApp(
    name="Stelvio app",
    modules=[
        # Need these in specific order? Just list them
        # "infra.base",
        # Don't care about the rest? Glob it!
        "*/infra/*.py",
        "**/*stlv.py",
    ],
)

app.run()

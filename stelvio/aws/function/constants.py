DEFAULT_ARCHITECTURE_DEVMODE = "arm64"
DEFAULT_MEMORY = 128
DEFAULT_TIMEOUT = 60
# stlv_resources.py: the on-disk copy is the folder union for the IDE; each Lambda gets its
# own generated copy as a StringAsset, or none
LAMBDA_EXCLUDED_FILES = [".DS_Store", "stlv_resources.py"]  # exact file matches
LAMBDA_EXCLUDED_DIRS = ["__pycache__"]
LAMBDA_EXCLUDED_EXTENSIONS = [".pyc"]
MAX_LAMBDA_LAYERS = 5
LAMBDA_BASIC_EXECUTION_ROLE = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
LAMBDA_VPC_ACCESS_EXECUTION_ROLE = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
)

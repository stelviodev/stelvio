DEFAULT_RUNTIME = "python3.12"
DEFAULT_ARCHITECTURE = "x86_64"
DEFAULT_MEMORY = 128
DEFAULT_TIMEOUT = 60
LAMBDA_EXCLUDED_FILES = ["stlv.py", ".DS_Store"]  # exact file matches
LAMBDA_EXCLUDED_DIRS = ["__pycache__"]
LAMBDA_EXCLUDED_EXTENSIONS = [".pyc"]
MAX_LAMBDA_LAYERS = 5
# "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
LAMBDA_BASIC_EXECUTION_ROLE = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
NUMBER_WORDS = {
    "0": "Zero",
    "1": "One",
    "2": "Two",
    "3": "Three",
    "4": "Four",
    "5": "Five",
    "6": "Six",
    "7": "Seven",
    "8": "Eight",
    "9": "Nine",
}

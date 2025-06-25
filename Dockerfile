# Use a slim Python 3.9 image as the base
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
# --no-cache-dir: Do not use the pip cache, reduces image size
# -r requirements.txt: Install packages listed in requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# Command to run the application when the container starts
# This will execute your main.py script
CMD ["python", "main.py"]

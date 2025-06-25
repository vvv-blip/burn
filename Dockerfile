# Use a slim Python 3.9 image as the base
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# Expose port 10000 for Flask
EXPOSE 10000

# Command to run the application
CMD ["python", "main.py"]

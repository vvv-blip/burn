{\rtf1\ansi\ansicpg1252\cocoartf2822
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 # Use a slim Python 3.9 image as the base\
FROM python:3.9-slim-buster\
\
# Set the working directory inside the container\
WORKDIR /app\
\
# Copy the requirements file and install dependencies\
# --no-cache-dir: Do not use the pip cache, reduces image size\
# -r requirements.txt: Install packages listed in requirements.txt\
COPY requirements.txt .\
RUN pip install --no-cache-dir -r requirements.txt\
\
# Copy the rest of your application code into the container\
COPY . .\
\
# Command to run the application when the container starts\
# This will execute your main.py script\
CMD ["python", "main.py"]\
}
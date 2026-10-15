# Employee Attendance & Analytics API

## Overview

This project implements an Employee Attendance & Analytics API using Python, FastAPI, and MongoDB.

The API supports employee records, attendance punch-in and punch-out, manual attendance corrections with an audit trail, and analytics endpoints for employee attendance, departments, late arrivals, and attendance trends.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

MongoDB Atlas or a local MongoDB instance can be used.

## Setup

Create and activate a virtual environment, then install the dependencies.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project root and configure your MongoDB connection:

```text
MONGO_URI=your_mongodb_connection_string
MONGO_DB=your_database_name
```

Replace the placeholder values with your own settings. Never commit `.env` or database credentials.

If you need to load the provided sample dataset, run:

```powershell
python sample_seed.py
```

Start the API:

```powershell
uvicorn app.main:app --port 8000
```

For automatic reload during development, use:

```powershell
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

With the server running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Analytics and Query Inspection

The API includes analytics endpoints for monthly employee attendance, department summaries, late-arrival rankings, and department attendance trends. An administrative endpoint exposes MongoDB explain information for supported query types.

Refer to the route definitions in `app/main.py` and the API contract for the supported parameters and response formats.
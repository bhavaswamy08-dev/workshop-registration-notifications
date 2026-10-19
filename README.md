# Workshop Registration and Notifications

## 1. Project Overview

The Workshop Registration and Notifications system is a backend application designed to manage workshops, members, registrations, and notifications.

The application uses FastAPI to provide REST APIs and SQLite to store application data. When a member registers for a workshop, the system creates a notification job in the database. A separate background worker processes these jobs and communicates with a mock notification service.

If notification delivery fails, the worker automatically schedules a retry. This approach allows registration requests to complete without waiting for notifications to be delivered.

## 2. Objectives

* Manage workshops and member information.
* Allow members to register for available workshops.
* Prevent duplicate registrations and enforce workshop capacity.
* Generate notification jobs for registrations and cancellations.
* Process notifications asynchronously using a background worker.
* Retry failed notifications and record delivery attempts.
* Maintain notification status and support recovery after worker restarts.

## 3. Technologies Used

| Technology | Purpose                             |
| ---------- | ----------------------------------- |
| Python     | Application development             |
| FastAPI    | REST API development                |
| SQLite     | Database storage                    |
| SQLAlchemy | Database operations and ORM         |
| HTTPX      | HTTP communication between services |
| Pytest     | Automated testing                   |
| Uvicorn    | Running the FastAPI applications    |

## 4. System Architecture

The application consists of three main components.

### 4.1 Main API

The main API manages workshops, members, and registrations. It validates requests, stores data, and creates notification jobs.

### 4.2 Background Notification Worker

The worker runs as a separate process. It retrieves eligible notification jobs, sends requests to the notification service, records delivery attempts, and schedules retries when necessary.

### 4.3 Mock Notification Service

The mock service simulates notification delivery. It supports successful responses and simulated failures, allowing the retry mechanism to be tested without sending real email or SMS messages.

### 4.4 Workflow

1. A member registers for a workshop.
2. The API validates the registration and saves it in the database.
3. A notification job is created and stored.
4. The background worker retrieves the job.
5. The worker sends the notification request to the mock service.
6. The result is recorded in the database.
7. If delivery fails, the worker schedules a retry according to the configured retry policy.
8. Once delivery succeeds, the job is marked as delivered.

## 5. Key Features

### Workshop Management

Create and manage workshops with details such as title, capacity, and status.

### Member Management

Create and maintain member records used during workshop registration.

### Registration Management

Register members for workshops while validating capacity and preventing duplicate registrations.

### Asynchronous Notifications

Notification jobs are stored in the database and processed independently by the background worker. The registration API does not need to wait for notification delivery.

### Automatic Retry Mechanism

When a temporary delivery failure occurs, the worker records the failed attempt and schedules another attempt according to the configured retry policy.

### Delivery Tracking

The system records notification status, attempt number, HTTP response status, error details, and delivery timestamps where applicable.

### Failure Handling and Recovery

The worker handles connection errors, timeouts, and HTTP failures. Database-backed jobs allow pending work to remain available when the worker restarts.

### Cancellation Notifications

Workshop cancellation can generate notification jobs for registered members.

### Idempotency

The application uses idempotency mechanisms to help prevent duplicate processing when requests or delivery attempts are repeated.

## 6. Project Structure

```text
workshop-registration/
├── app/
│   ├── api/
│   ├── services/
│   ├── worker/
│   │   └── notification_worker.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── models.py
│   └── schemas.py
├── mock_service/
│   ├── main.py
│   ├── database.py
│   └── models.py
├── tests/
├── .env.example
├── requirements.txt
├── pytest.ini
└── README.md
```

The `app` directory contains the main application, API routes, services, database models, and background worker. The `mock_service` directory contains the simulated notification service, while `tests` contains automated tests.

## 7. Installation and Setup

### Prerequisites

* Python installed on your system.
* Visual Studio Code or another code editor.
* A terminal for running the application.

### Step 1: Install Dependencies

Open a terminal in the project directory and run:

```bash
py -m pip install -r requirements.txt
```

### Step 2: Start the Mock Notification Service

Open a terminal in the project directory and run:

```bash
py -m uvicorn mock_service.main:app --port 8001
```

Keep this terminal running.

### Step 3: Start the Main API

Open another terminal in the same project directory and run:

```bash
py -m uvicorn app.main:app --port 8000
```

Keep this terminal running as well.

### Step 4: Start the Background Worker

Open a third terminal and run:

```bash
py -m app.worker.notification_worker
```

The worker will continue checking for eligible notification jobs and processing them.

**Note:** Run these commands from the inner `workshop-registration` directory that contains `app`, `mock_service`, and `requirements.txt`.

## 8. API Documentation

FastAPI provides interactive documentation for exploring and testing the API endpoints.

* **Main API:** http://127.0.0.1:8000/docs
* **Mock Notification Service:** http://127.0.0.1:8001/docs

The main API includes operations for workshop management, member management, registrations, and notification tracking.

The notification endpoints include:

* `GET /notifications` — Retrieve notification jobs.
* `GET /notifications/summary` — View notification status summaries.
* `GET /notifications/{job_id}` — Retrieve details of a specific notification job.

Use the interactive documentation to inspect available endpoints and their request formats.

## 9. Testing

The project uses Pytest for automated testing.

Run the complete test suite with:

```bash
py -m pytest -v
```

The tests cover application functionality, registration behavior, notification processing, failure handling, and retry behavior.

**Latest test result:** 92 tests passed in the tested environment, with one deprecation warning from the testing framework integration.

## 10. Example Notification Retry Scenario

The retry mechanism was verified using the mock notification service.

| Attempt        | HTTP Response             | Result                           |
| -------------- | ------------------------- | -------------------------------- |
| First attempt  | 500 Internal Server Error | Delivery failed; retry scheduled |
| Second attempt | 201 Created               | Delivery succeeded               |

The worker logs confirmed that the first attempt failed and the second attempt succeeded. The notification job's API response also showed both delivery attempts, including their outcomes and HTTP status codes.

This demonstrates how the application handles a temporary notification failure without losing the registration or notification job.
## 11. Worker Restart Recovery

The notification worker processes notification jobs stored in the database. Because these jobs are persisted, pending notifications remain available even if the worker stops unexpectedly or the application process restarts.

### Recovery Scenario

1. Start the main API, mock notification service, and background worker.

2. Register a member for a workshop.

3. Verify that the registration is saved and a notification job is created.

4. Stop the background worker before the pending notification is successfully delivered.

5. Restart the worker using:

   ```bash
   py -m app.worker.notification_worker
   ```

6. Check the notification status using the notification API endpoints.

7. Verify that the worker processes the pending job and updates its status after delivery.

### Expected Result

The registration remains stored in the database, and the pending notification job remains available after the worker restarts. The worker can resume processing eligible jobs instead of losing them when its process stops.

**Verification:** Record the notification job ID and its status before stopping the worker. After restarting it, check the same job ID and confirm whether the notification was delivered. Include the observed result in the project demonstration.

## 12. Lost Notifications, Retries, and Duplicate Delivery

### Lost Notifications

A notification can be lost if a system creates a registration but does not reliably save the associated notification job. A similar problem can occur if a job is marked as completed before the notification service actually receives it.

**How this project addresses the problem:** The application stores notification jobs in the database so that eligible pending jobs can be processed independently of the registration request. This reduces the risk of losing notification work when the worker stops.

### Retry Mechanism

The notification service may return an error or time out during delivery. The worker records the failed attempt and schedules another attempt according to the configured retry policy.

For example:

| Attempt        | Result           | Action                              |
| -------------- | ---------------- | ----------------------------------- |
| First attempt  | HTTP 500 error   | Record failure and schedule a retry |
| Second attempt | HTTP 201 success | Mark the notification as delivered  |

This allows temporary failures to be handled without requiring the member to register again.

### Duplicate Delivery

Duplicate delivery can occur when the notification service successfully receives a notification but the worker does not receive the success response—for example, because of a network timeout. The worker may retry the job because it cannot determine whether the first attempt succeeded.

**How this project addresses the problem:** The application uses idempotency mechanisms to help prevent duplicate processing. However, preventing duplicate delivery completely depends on how delivery attempts and the receiving service are implemented. A retry alone cannot guarantee exactly-once delivery.

### Summary

Persistent notification jobs, retry handling, and delivery-status tracking improve reliability. However, the system must account for network failures, worker restarts, and uncertain delivery outcomes. These scenarios should be tested to verify the behavior of the implemented recovery and idempotency mechanisms.

## 13. Limitations

* The notification service is a mock service and does not send real email or SMS messages.
* SQLite is used for local database storage.
* The application is intended to demonstrate backend development, asynchronous processing, and retry handling.
* Production deployment may require additional security, monitoring, and operational configuration.

## 14. Conclusion

The Workshop Registration and Notifications application demonstrates how a backend system can combine REST APIs, database transactions, background processing, and automatic retries.

By storing notification jobs independently of delivery, the application can handle temporary failures while maintaining registration records and tracking notification outcomes. The automated test suite provides additional verification of the application's behavior.

# ---- Build stage ----
FROM python:3.11-slim AS builder

# Install build‑time dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gcc

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app

# Copy packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY . .

# Expose port 8080
ENV PORT=8080
EXPOSE 8080

# Start Flask app
CMD ["python", "-m", "flask", "--app", "app", "run", "--host", "0.0.0.0"]

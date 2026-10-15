# ─────────────────────────────────────────────
# Stage 1: Build the React frontend
# ─────────────────────────────────────────────
FROM node:22-slim AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --legacy-peer-deps

COPY frontend/ ./
RUN npm run build

# ─────────────────────────────────────────────
# Stage 2: Final image (Node + Python together)
# ─────────────────────────────────────────────
FROM node:22-slim

# Install Python + system deps + git
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    graphviz \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy built frontend + node_modules
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist
COPY --from=frontend-builder /app/frontend/node_modules ./frontend/node_modules
COPY --from=frontend-builder /app/frontend/package.json ./frontend/package.json

# Copy Python backend & config files
COPY codegate/ ./codegate/
COPY pyproject.toml ./
COPY requirements.txt ./

# Clone Scalpel inside image (since frontend/Scalpel is gitignored)
RUN git clone https://github.com/SMAT-Lab/Scalpel.git frontend/Scalpel

# Install Python deps + codegate package
RUN python3 -m pip install --no-cache-dir -r requirements.txt
RUN python3 -m pip install --no-cache-dir -e .

# Tell the Express bridge to spawn python3 and find Scalpel
ENV CODEGATE_PYTHON=python3
ENV PYTHONPATH=/app/frontend/Scalpel/src
ENV NODE_ENV=production
ENV PORT=3000

EXPOSE 3000

# Start the Express server (serves React + proxies analysis to Python)
CMD ["node", "frontend/dist/index.js"]

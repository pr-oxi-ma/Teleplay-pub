#!/bin/bash

echo "🚀 Starting TelePlay Deployment..."

# Function to check if a screen session is already running
manage_service() {
    SERVICE_NAME=$1
    if screen -list | grep -q "\.${SERVICE_NAME}"; then
        echo "🛑 Existing '${SERVICE_NAME}' process found. Stopping it..."
        screen -S "${SERVICE_NAME}" -X quit
        sleep 2 # Give the process time to shut down properly and free up the port
    else
        echo "✅ No existing '${SERVICE_NAME}' process found. Starting fresh."
    fi
}

# ==========================================
# 1. BACKEND DEPLOYMENT
# ==========================================
echo "----------------------------------------------------"
echo "⚙️ Setting up Backend..."
manage_service "backend"

cd backend || exit

# (Note: You may be prompted for your password here due to 'sudo')
sudo apt install python3-dev build-essential -y
mkdir -p data session

echo "📦 Installing Backend Dependencies..."
pip install -r requirements.txt

echo "🚀 Starting Backend in the background..."
screen -dmS backend python3 run.py
cd ..


# ==========================================
# 2. FRONTEND DEPLOYMENT
# ==========================================
echo "----------------------------------------------------"
echo "🌐 Setting up Frontend..."
manage_service "web"

cd web || exit

# Check if Bun is installed; if not, install it.
if [ ! -d "$HOME/.bun" ]; then
    echo "🔧 Bun not found. Installing Bun..."
    curl -fsSL https://bun.sh/install | bash
else
    echo "⚡ Bun is already installed. Skipping installation."
fi

# Explicitly add Bun to the script's path.
export PATH="$HOME/.bun/bin:$PATH"

echo "📦 Installing Frontend Dependencies..."
bun install

echo "🚀 Starting Frontend in the background..."
screen -dmS web bun run dev
cd ..


# ==========================================
# 3. FINISH
# ==========================================
echo "----------------------------------------------------"
echo "🎉 Deployment Complete! Both servers are running."
echo "👉 To view Backend logs:  screen -r backend"
echo "👉 To view Frontend logs: screen -r web"
echo "👉 To exit a screen session: Press CTRL+A, then D"
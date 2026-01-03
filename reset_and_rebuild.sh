#!/bin/bash
# Complete reset and rebuild script for AI-Agent

echo "=== AI-Agent Complete Reset & Rebuild ==="
echo ""

# Ask for confirmation
read -p "This will clear ALL agent memory and rebuild the container. Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]
then
    echo "Aborted."
    exit 1
fi

echo "Step 1: Stopping and removing container..."
docker stop agent-app 2>/dev/null
docker rm agent-app 2>/dev/null

echo "Step 2: Clearing persistent memory files..."
sudo rm -f /mnt/docker-data/PROJECTS/AI-Agent/keys/execution_log.txt
sudo rm -f /mnt/docker-data/PROJECTS/AI-Agent/keys/execution_log_llm_context.txt
sudo rm -f /mnt/docker-data/PROJECTS/AI-Agent/keys/chat_history.json
sudo rm -f /mnt/docker-data/PROJECTS/AI-Agent/keys/action_plan.json
echo "[]" | sudo tee /mnt/docker-data/PROJECTS/AI-Agent/keys/chat_history.json > /dev/null
echo "{}" | sudo tee /mnt/docker-data/PROJECTS/AI-Agent/keys/action_plan.json > /dev/null

echo "Step 3: Fixing session.json if needed..."
if [ -d /mnt/docker-data/PROJECTS/AI-Agent/session.json ]; then
    sudo rm -rf /mnt/docker-data/PROJECTS/AI-Agent/session.json
fi
echo '{}' | sudo tee /mnt/docker-data/PROJECTS/AI-Agent/session.json > /dev/null

echo "Step 4: Rebuilding Docker image..."
docker build -t agent-controller .

if [ $? -ne 0 ]; then
    echo "ERROR: Docker build failed!"
    exit 1
fi

echo "Step 5: Starting fresh container..."
docker run -d --name agent-app -p 5000:5000 \
  -v /mnt/docker-data/PROJECTS/AI-Agent/keys:/app/keys \
  -v /mnt/docker-data/PROJECTS/AI-Agent/session.json:/app/session.json \
  agent-controller

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start container!"
    exit 1
fi

echo ""
echo "=== Reset Complete! ==="
echo "Container is starting... Waiting 3 seconds..."
sleep 3

echo ""
echo "Checking logs:"
docker logs agent-app --tail 10

echo ""
echo "Application should be available at: http://localhost:5000"
echo ""

AI SSH Controller
=================

AI SSH Controller is a lightweight web app that allows you to control remote systems via SSH using a web interface and an AI instance.
You only need to set the objective and the AI will do the work for you.

This project is build entirely using AI for coding. I am not a programmer. My coding skills are at "Hello world!" level.
Feel free to improve it or modify it as you like.

Installation Guide
------------------

Follow these steps to install and run the AI SSH Controller on a Linux server using Docker.

1. Required Files
-----------------
Ensure the following files and directories are placed in a single folder on your server (e.g., /docker/ai-agent):

- app.py
- Dockerfile
- config.ini (configuration file)     `cp config.ini.template config.ini`
- .dockerignore
- templates/ (directory containing HTML files)

2. Requirements
---------------
- A Linux-based OS
- Docker installed

3. Build and Run
----------------

Step 1: Build the Docker image

Navigate to the project directory and run the build command:

    cd /path/to/project
    docker build -t ai-agent-controller .

Step 2: Start the Docker container

Run the following command to start the application:

    docker run -d --name ai-agent -p 5001:5000 -v $(pwd):/app --restart unless-stopped ai-agent-controller

Explanation:
- -d: Runs the container in detached mode (background)
- --name ai-agent: Names the container for easier management
- -p 5001:5000: Maps port 5001 on the host to port 5000 in the container (you can change 5001 if needed)
- -v "$(pwd):/app": Mounts the current directory to /app in the container to ensure data persistence.
   You can replace "$(pwd)" with any path you want
- --restart unless-stopped: Automatically restarts the container unless manually stopped

4. First-Time Setup
-------------------

1. Access the Interface: Open your browser and go to http://<server-ip>:5001
2. SSH Key Generation: On first launch, the app automatically generates an SSH key pair. The public key is visible in the web interface under "Remote System Connection"
3. Configuration:
   - Set up the LLM connection (Ollama or Gemini)
   - Use the interface to configure and deploy the SSH key to the remote systems you want to control. You only need the password once to allow the app to set up the SSH key, after that the key will be used to authenticate and run the commands.
   - Select the execution mode. Independent or assisted.
       * Independent - the LLM will generate and run commands without human intervention until: the objective is met, the max steps is reached or the stop button is pressed 
       * Assisted  - the LLM will generate the commands but it will not be executed until the user aprove them one by one.
         
** Important note **
---------------------
  Please test it in a virtual machine or on non production systems as the AI can generate distructive commands.
  
  For local Ollama LLM at this moment the gpt-oss:20b seems to be the best choice.

---------------------

5. Useful Docker Commands
-------------------------

- View logs in real-time:
   `docker logs -f ai-agent`

- Stop the application:
      `docker stop ai-agent`

- Start the application (after stopping):
      `docker start ai-agent`

- Delete the container (for reinstalling):
      `docker stop ai-agent`
      `docker rm ai-agent`

- Update the application:
  1. Stop and remove the old container
  2. Replace source files (e.g., app.py) with the new version
  3. Rebuild the image:
         `docker build -t ai-agent-controller`
  4. Restart the container using the same docker run command. Your data (keys, configs) will be preserved thanks to the mounted volume.

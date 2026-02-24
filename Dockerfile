####################################################################################################
## Builder image
####################################################################################################

# This sets up the base image that we are going to be using
# This is a slimmed down version of Debian 11 (GCC 10)
# This image is a builder base, and is what allows Docker to do these steps faster
FROM python:3.12.12-slim-trixie AS builder

# Prevents packages from attempting to ask for user info for configs
ENV DEBIAN_FRONTEND=noninteractive 

# Install build tools
RUN apt update \
  && apt install -y --no-install-recommends \
  build-essential \
  ca-certificates

# Here we'll create a working directory and cd into it

WORKDIR /bot

# Copy over the requirements.txt from the base of the repo
# The build context is '.' (which is the root of this repo)
COPY /requirements.txt /bot/

# Instead of wasting time installing and building the wheels later, we'll export the requirements.txt libs as wheels to save time
# This move is also cached, which saves a ton of time later on
RUN pip wheel --wheel-dir=/bot/wheels -r requirements.txt

####################################################################################################
## Final image
####################################################################################################

# Use the same base image again
FROM python:3.12.12-slim-trixie

# Install any non-build tool packages, and just the stuff needed to run
# Tini - Our PID1 for this container, which will be the init. Tini prevents creating zombie processes and forwards signals properly
# ca-certificates - Needed in case of any SSL connections that might be used
# bash - Our shell for this container 
RUN apt update \
  && apt install -y --no-install-recommends \
  tini \
  ca-certificates \
  bash 

# We'll change or create the directory bot again, and copy any files we need for the bot to work
# Copy over discord bot file and cogs
COPY /bot.py /bot/bot.py
COPY /cogs /bot/cogs
COPY /storage /bot/storage
COPY /requirements.txt /bot/

# Copy over our start shell file. This will be used to create environment variables for the token of the bot
COPY /scripts/start.sh /bot/start.sh

# Copy over our wheels from the builder stage
COPY --from=builder /bot/wheels /bot/wheels

# Upgrade both pip and setuptools to the latest version. This will fix any issues with installing the wheels, and is good practice to do this as well
RUN pip install --upgrade pip setuptools

# Currently when we run something, it's being ran as root
# Running your app as root is dangerous, and can cause issues with permissions, and be a massive security risk
# So we'll create a new user that doesn't have any power to do much except to run the app
# and set everything up that way. Note that before this step runs, all files are still owned by root
RUN adduser --disabled-password --gecos "" discordbot \
  && chown -R discordbot:discordbot /bot \
  && chmod +x /bot/start.sh

# Change into the user
USER discordbot

# This is to add any executeables that is needed for any programs to run. Normally if you are running a web app w/ gunicorn, you'll need this step
# But we don't need it for this bot, but we'll have it here to stop pip from complaining again
ENV PATH="${PATH}:/home/discordbot/.local/bin"


# Now we finally install all of our  dependencies
RUN pip install --user --no-index --find-links=/bot/wheels -r /bot/requirements.txt

# Set up tini
ENTRYPOINT ["/usr/bin/tini", "--"]

# And this will be the command that gets ran
# This is the first one after tini that will get ran
CMD ["/bot/start.sh"]

# Let tini handle the work of default singals, and if the container stops, we'll safely close the process w/ tini
STOPSIGNAL SIGTERM

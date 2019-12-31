#!/usr/bin/env bash
set -e

if [[ -z "$1" ]]; then
    echo "Usage: send_notification.sh TEXT siteId"
fi

text="$1"
siteId="$2"
if [[ -z "${siteId}" ]]; then
    siteId="default"
fi

# -----------------------------------------------------------------------------

jq -n --arg text "${text}" --arg siteId "${siteId}" \
   '{ siteId:$siteId, init:{ type:"notification", text:$text } }' | \
    mosquitto_pub -t 'hermes/dialogueManager/startSession' -s

import json
from flask import Flask, render_template, request, jsonify
import subprocess
import re
import os
import shlex

app = Flask(__name__)

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def get_interfaces():
    # Identify ethernet and wifi interfaces using nmcli
    _, out = run_cmd("nmcli -t -f DEVICE,TYPE device status")
    interfaces = {"ethernet": [], "wifi": []}
    if out:
        for line in out.strip().split("\n"):
            parts = line.split(":")
            if len(parts) >= 2:
                dev, dev_type = parts[0], parts[1]
                if dev_type == "ethernet":
                    interfaces["ethernet"].append(dev)
                elif dev_type == "wifi":
                    interfaces["wifi"].append(dev)
    return interfaces

def get_bridge_status():
    success, out = run_cmd("nmcli -t -f NAME,TYPE,STATE connection show --active")
    if success and out:
        for line in out.strip().split("\n"):
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == "br0" and "bridge" in parts[1]:
                return True
    return False

def clean_bridge():
    # Attempt to delete old bridge structures if they exist
    run_cmd("nmcli connection down br0")
    run_cmd("nmcli connection delete br0")
    run_cmd("nmcli connection delete br-port-eth")
    run_cmd("nmcli connection delete br-port-wifi")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    interfaces = get_interfaces()
    active = get_bridge_status()
    return jsonify({
        "interfaces": interfaces,
        "is_active": active
    })

@app.route("/api/scan_wifi", methods=["GET"])
def api_scan_wifi():
    wifi_dev = request.args.get("wifi")
    cmd = f"nmcli -t -f SSID device wifi list ifname {shlex.quote(wifi_dev)}" if wifi_dev else "nmcli -t -f SSID device wifi list"
    success, out = run_cmd(cmd)
    ssids = set()
    if success and out:
        for line in out.strip().split("\n"):
            ssid = line.strip()
            # Unescape backslash-escaped characters nmcli uses
            ssid = ssid.replace("\\:", ":")
            if ssid and ssid != "--":
                ssids.add(ssid)
    return jsonify({"ssids": sorted(list(ssids))})

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json
    eth_dev = data.get("ethernet")
    wifi_dev = data.get("wifi")
    ssid = data.get("ssid")
    password = data.get("password")
    band = data.get("band", "bg") # 'bg' is 2.4GHz, 'a' is 5GHz

    if not all([eth_dev, wifi_dev, ssid, password]):
        return jsonify({"success": False, "error": "Missing parameters"}), 400
        
    if len(password) < 8 or len(password) > 63:
        return jsonify({"success": False, "error": "Password must be manually set between 8 and 63 characters"}), 400

    eth_dev_q = shlex.quote(eth_dev)
    wifi_dev_q = shlex.quote(wifi_dev)
    ssid_q = shlex.quote(ssid)
    password_q = shlex.quote(password)

    clean_bridge()
    
    # 1. Create bridge
    cmd1 = 'nmcli connection add type bridge con-name br0 ifname br0 ipv4.method auto ipv6.method auto'
    s1, err1 = run_cmd(cmd1)
    if not s1:
        return jsonify({"success": False, "error": f"Failed to create bridge: {err1}"}), 500

    # 2. Add Ethernet slave
    cmd2 = f'nmcli connection add type ethernet slave-type bridge con-name br-port-eth ifname {eth_dev_q} master br0'
    s2, err2 = run_cmd(cmd2)
    if not s2:
        clean_bridge()
        return jsonify({"success": False, "error": f"Failed to add ethernet slave: {err2}"}), 500

    # 3. Add WiFi AP slave
    # Note: wifi-sec.key-mgmt wpa-psk uses WPA2 by default
    # Note: 802-11-wireless.powersave 2 forcefully disables speed-throttling power management
    cmd3 = f'nmcli connection add type wifi slave-type bridge con-name br-port-wifi ifname {wifi_dev_q} master br0 wifi.mode ap wifi.ssid {ssid_q} wifi-sec.key-mgmt wpa-psk wifi-sec.psk {password_q} 802-11-wireless.powersave 2'
    
    if band == "a":
        # Force 5GHz on channel 149 (Bypasses the "No IR" lock common on lower channels)
        cmd3 += " 802-11-wireless.band a 802-11-wireless.channel 149"
    elif band == "bg":
        # Force 2.4GHz on channel 6
        cmd3 += " 802-11-wireless.band bg 802-11-wireless.channel 6"

    s3, err3 = run_cmd(cmd3)
    if not s3:
        clean_bridge()
        return jsonify({"success": False, "error": f"Failed to add wifi slave. Your card may not support the selected band/AP mode: {err3}"}), 500

    # 4. Bring up the bridge
    s4, err4 = run_cmd('nmcli connection up br0')
    if not s4:
        clean_bridge()
        return jsonify({"success": False, "error": f"Failed to bring up the bridge: {err4}"}), 500

    return jsonify({"success": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    clean_bridge()
    # It might be necessary to bring the old connections back up
    interfaces = get_interfaces()
    
    eths = interfaces["ethernet"]
    if eths:
      run_cmd(f"nmcli device connect {shlex.quote(eths[0])}")
      
    wifis = interfaces["wifi"]
    if wifis:
      run_cmd(f"nmcli device connect {shlex.quote(wifis[0])}")

    return jsonify({"success": True})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)

import json
from flask import Flask, render_template, request, jsonify
import subprocess
import re
import os
import shlex

app = Flask(__name__)

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, str(e.output)

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
    run_cmd("pkill hostapd")
    for w in get_interfaces()["wifi"]:
        run_cmd(f"nmcli dev set {shlex.quote(w)} managed yes")
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

    # 3. Seize hardware control from NetworkManager
    run_cmd("rfkill unblock wlan")
    run_cmd(f"nmcli dev set {wifi_dev_q} managed no")

    # 4. Bring up the bridge so it's ready for Hostapd
    s4, err4 = run_cmd('nmcli connection up br0')
    if not s4:
        clean_bridge()
        return jsonify({"success": False, "error": f"Failed to bring up the bridge: {err4}"}), 500

    # 5. Optimize bridge for raw throughput (disable STP delay, set forward delay to 0)
    run_cmd("ip link set br0 type bridge stp_state 0")
    run_cmd("ip link set br0 type bridge forward_delay 0")

    # 6. Kernel-level network stack tuning for maximum throughput
    run_cmd("sysctl -w net.core.rmem_max=16777216")
    run_cmd("sysctl -w net.core.wmem_max=16777216")
    run_cmd("sysctl -w net.core.rmem_default=1048576")
    run_cmd("sysctl -w net.core.wmem_default=1048576")
    run_cmd("sysctl -w net.core.netdev_max_backlog=5000")
    run_cmd("sysctl -w net.ipv4.tcp_rmem='4096 1048576 16777216'")
    run_cmd("sysctl -w net.ipv4.tcp_wmem='4096 1048576 16777216'")
    run_cmd("sysctl -w net.ipv4.tcp_fastopen=3")
    run_cmd("sysctl -w net.ipv4.tcp_mtu_probing=1")

    # 7. Build dynamic hostapd configuration engine
    _, reg_out = run_cmd("iw reg get")
    country = "US"
    if reg_out:
        match = re.search(r"country ([A-Z]{2}):", reg_out)
        if match:
            country = match.group(1)

    conf = f"""interface={wifi_dev}
bridge=br0
driver=nl80211
ssid={ssid}
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
wmm_enabled=1
ieee80211n=1
country_code={country}
ieee80211d=1
ieee80211h=1
preamble=1
max_num_sta=32
"""
    if band == "a":
        conf += """hw_mode=a
channel=149
ieee80211ac=1
ieee80211ax=1
vht_oper_chwidth=1
vht_oper_centr_freq_seg0_idx=155
ht_capab=[HT40+][LDPC][SHORT-GI-20][SHORT-GI-40][TX-STBC][RX-STBC1][MAX-AMSDU-7935]
vht_capab=[MAX-MPDU-11454][RXLDPC][SHORT-GI-80][SHORT-GI-160][TX-STBC-2BY1][SU-BEAMFORMEE][MU-BEAMFORMEE][VHT160]
"""
    else:
        conf += """hw_mode=g
channel=6
ieee80211ax=1
ht_capab=[HT40+][LDPC][SHORT-GI-20][SHORT-GI-40][TX-STBC][RX-STBC1][MAX-AMSDU-7935]
"""

    with open("/tmp/wifi_amp_hostapd.conf", "w") as f:
        f.write(conf)

    # 8. Ignite raw hostapd daemon
    s5, err5 = run_cmd('hostapd /tmp/wifi_amp_hostapd.conf -B')
    if not s5:
        clean_bridge()
        return jsonify({"success": False, "error": f"Failed to ignite raw hostapd engine. Ensure hostapd is installed and the app is running under sudo! {err5}"}), 500

    # 9. Post-launch hardware optimizations (must happen AFTER hostapd starts)
    run_cmd(f"iw dev {wifi_dev} set power_save off")
    run_cmd(f"iw dev {wifi_dev} set txpower fixed 2200")

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

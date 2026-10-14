// Print one CSV row of Wi-Fi stats: rssi,noise,channel,band,width,tx_rate
// The BSSID needs location permission, so it isn't included.
import CoreWLAN

guard let iface = CWWiFiClient.shared().interface(), iface.powerOn() else {
    print(",,,,,")
    exit(0)
}

let ch = iface.wlanChannel()
let band: String
switch ch?.channelBand {
case .band2GHz: band = "2.4"
case .band5GHz: band = "5"
case .band6GHz: band = "6"
default: band = ""
}
let width: String
switch ch?.channelWidth {
case .width20MHz: width = "20"
case .width40MHz: width = "40"
case .width80MHz: width = "80"
case .width160MHz: width = "160"
default: width = ""
}
let connected = ch != nil
print([
    connected ? String(iface.rssiValue()) : "",
    connected ? String(iface.noiseMeasurement()) : "",
    ch.map { String($0.channelNumber) } ?? "",
    band,
    width,
    connected ? String(iface.transmitRate()) : "",
].joined(separator: ","))

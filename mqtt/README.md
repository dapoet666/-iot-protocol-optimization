# MQTT – Nhóm 11 (Duy Anh)

## Phạm vi
Đây là phần MQTT dành cho mô phỏng IoT: `sensor.py` (h1..hN) → UDP/5000 → `hg` (`gw_mqtt.py`) → TCP MQTT/1883 → `server` (Mosquitto + `srv_mqtt.py`) → CSV.

**Trạng thái:** đã kiểm tra cú pháp, cấu trúc dữ liệu, và thử logic bằng MQTT client giả lập. **CHƯA chạy end-to-end với Mosquitto và Mininet thật**, nên chưa có kết quả CSV/PCAP thực nghiệm để báo cáo.

## Quy ước tích hợp
- Gateway `hg-eth0`: `10.0.1.1/24`, nhận UDP port `5000`.
- Broker trên server: `10.0.3.2:1883`.
- Payload: `struct.pack("!HIf", sensor_id, seq, value)`, 10 byte, big endian.
- MQTT topic: `f1/s1/t`; gateway hỗ trợ QoS 0 và QoS 1.
- CSV kết quả: `sensor_id,seq,recv_epoch_ns`.
- Ghép file sensor CSV theo hai trường `sensor_id, seq`, đối chiếu với `send_epoch_ns`.

## Cài đặt trên Ubuntu
```bash
sudo apt install -y mosquitto mosquitto-clients tcpdump python3-venv
python3 -m venv ~/iot
~/iot/bin/pip install -r mqtt/requirements.txt
```

## Chạy thử trong Mininet
Khởi chạy topology trong Ubuntu, ví dụ `sudo python3 'topo (1).py' -n 20`. Các câu lệnh sau chạy trong Mininet CLI. Thay `/ABS/PATH` bằng đường dẫn tuyệt đối đến project và `USER` bằng tên tài khoản Linux. Tạo thư mục `results` trước khi chạy.

```text
mininet> server mosquitto -c /ABS/PATH/mqtt/mosq.conf -v &
mininet> server /home/USER/iot/bin/python /ABS/PATH/mqtt/srv_mqtt.py --broker 127.0.0.1 --run-id m01 --out /ABS/PATH/results/m01.server.csv &
mininet> hg /home/USER/iot/bin/python /ABS/PATH/mqtt/gw_mqtt.py --server 10.0.3.2 --qos 0 &
mininet> hg tcpdump -i hg-eth1 -w /ABS/PATH/results/m01.pcap tcp port 1883 &
mininet> h1 python3 /ABS/PATH/sensor.py --id 1 --duration 60 --log-dir /ABS/PATH/results
```

**Quan trọng:** chỉ bật cảm biến khi subscriber đã thông báo `Subscription confirmed` và gateway đã thông báo `Connected`. Chạy lại với `--qos 1`, `--run-id m02`, và tên file CSV/PCAP khác. Dừng các chương trình nền sau mỗi thí nghiệm.

Kỳ vọng với 1 cảm biến gửi mỗi giây trong 60 giây: 60 dòng bản tin + 1 dòng tiêu đề, nếu không mất dữ liệu ở tầng ứng dụng. Đây là **kỳ vọng**, không phải kết quả đo.

## Kiểm chứng và giới hạn
- `python3 -m py_compile mqtt/gw_mqtt.py mqtt/srv_mqtt.py`.
- Các test cục bộ xác minh bản tin `!HIf`, chuyển tiếp byte nguyên vẹn, loại bản tin sai kích thước và ghi CSV. Chúng **không thay thế** việc chạy Mosquitto và Mininet thật.
- QoS 1 không bảo đảm chặng UDP sensor → gateway; có thể có bản tin trùng.
- Cần lưu PCAP, CSV, log và kiểm tra số tin đã gửi/nhận khi triển khai thật.
- `mosq.conf` cho phép kết nối không xác thực; **chỉ dùng trong môi trường thí nghiệm cô lập**, không đưa broker ra Internet.

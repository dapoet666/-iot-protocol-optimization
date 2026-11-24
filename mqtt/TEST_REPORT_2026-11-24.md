# Biên bản kiểm tra MQTT – 24/11/2026

**Phụ trách:** Phạm Duy Anh  
**Tình trạng:** Kiểm thử cục bộ thành công; **A3 chưa hoàn thành do chưa chạy end-to-end trên Mininet**.

## Đã chạy kiểm thử
```bash
python -m compileall -q mqtt
python -m unittest discover -s mqtt/tests -v
python -m py_compile sensor.py "topo (1).py"
```
**Kết quả thực tế:** 3 tests, OK (0.537 giây). 
- Định dạng bản tin !HIf đúng 10 byte.
- Gateway chuyển nguyên vẹn dữ liệu và loại bản tin lỗi.
- Subscriber ghi CSV chính xác (sensor_id,seq,recv_epoch_ns) và loại bản tin lỗi.
- Đối chiếu tĩnh khớp sensor UDP/5000, gateway 10.0.1.1, broker 10.0.3.2 và QoS 0/1.

**Quan trọng:** Các test dùng MQTT client giả lập, **KHÔNG sử dụng Mosquitto/Mininet thật**. Môi trường kiểm tra không có Mininet, Mosquitto, Paho MQTT hoặc tcpdump. Chưa có kết quả đo 60 giây, CSV thực tế, PCAP hay thí nghiệm QoS 0 so với QoS 1.

## Cần làm để hoàn tất A3/A4
1. Trên Ubuntu + Mininet, kiểm tra kết nối h1→hg→server.
2. Chạy Mosquitto và subscriber trên node server; chờ "Subscription confirmed".
3. Chạy gw_mqtt.py trên hg; chờ "Connected"/"Receiving sensor UDP".
4. Bắt PCAP trên hg-eth1; chạy sensor.py từ h1 trong 60 giây. So sánh CSV nhận với log gửi; lưu cả log lỗi.
5. Chạy 2 lần tách biệt QoS 0 và QoS 1, giữ file CSV/PCAP cho mỗi lần.
6. Chỉ đánh dấu Xong khi có bằng chứng chạy thật.

## Đối chiếu góp ý giảng viên
- QoS 0/1/2 thuộc MQTT; CON/NON thuộc CoAP. Mã MQTT chỉ hỗ trợ QoS 0/1 ở phần này.
- "Chất lượng mạng" cần các phép đo/giả định RTT, mất gói, jitter và băng thông; gateway MQTT hiện chỉnh QoS thủ công, chưa tự đo rồi điều chỉnh.
- Cần đo overhead bản tin/handshake/CPU/RAM nếu kết luận về TLS/DTLS. mosq.conf hiện **không mã hóa và chỉ được dùng trong phòng thí nghiệm cô lập**.
- Mininet không phản ánh đầy đủ CPU/RAM của ESP32 hay Raspberry Pi.
- topology/topo.py hiện chưa cấu hình netem/tbf tạo RTT/loss/jitter; cần bổ sung trước K1/K2/K4.

**Trạng thái đề nghị:** A2 = mã nguồn và offline tests đã có; A3 = Đang làm (chưa có thí nghiệm thực); A4 = Chưa xong (hạn 26/11).

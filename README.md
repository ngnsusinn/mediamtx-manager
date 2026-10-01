# MediaMTX Manager

Relay RTSP bằng [MediaMTX](https://github.com/bluenviron/mediamtx) kèm web UI quản lý camera (thêm/sửa/xóa, bật/tắt, xem trạng thái, copy link). Chạy được bằng `docker compose` hoặc làm project trong **Arcane**.

## Cách hoạt động
- `mediamtx`: kéo stream từ server gốc (TCP), tự nối lại khi bị ngắt, phát lại trong LAN qua RTSP/HLS/WebRTC.
- `manager`: giữ danh sách camera trong `cameras.json` (volume `manager_data`) và đẩy vào MediaMTX qua Control API. Cứ mỗi `RECONCILE_INTERVAL` giây nó đối chiếu lại, nên MediaMTX restart cũng không mất camera.

## Chạy nhanh
```bash
cp .env.example .env     # sửa PUBLIC_HOST, WEBRTC_HOST, MANAGER_PASSWORD
docker compose up -d --build
```
Mở `http://<IP>:8080`.

## Chạy trên Arcane
Compose đã nhúng sẵn config MediaMTX (`configs.content`), dùng named volume nên không cần mount file.
1. Cách đơn giản: copy **cả thư mục repo** vào thư mục projects của Arcane (hoặc clone vào đó), Arcane sẽ nhận `compose.yaml` + `.env` và tự build `manager/`.
2. Cách không cần build trên máy đích: push repo lên GitHub, workflow `.github/workflows/image.yml` build image đa kiến trúc (amd64 + arm64, hợp TX3 mini) lên GHCR. Trong Arcane đặt `MANAGER_IMAGE=ghcr.io/<user>/mediamtx-manager:latest` trong Environment rồi dán `compose.yaml`.
3. Đặt biến trong Environment: `PUBLIC_HOST`, `WEBRTC_HOST` (IP LAN của máy chạy), `MANAGER_PASSWORD`.

## Thêm camera
- Trong UI: nhập path (vd `cam33`) + link RTSP. Password có `@` thì mã hóa thành `%40`.
- Hoặc dán nguyên JSON trả về từ app (`{"success":true,"data":[...]}`) vào ô **Nhập JSON**; mỗi camera thành path `cam<ID>`.

Xem stream: `rtsp://<IP>:8554/cam33` (VLC, OpenCV, ffmpeg), `http://<IP>:8889/cam33` (WebRTC), `http://<IP>:8888/cam33` (HLS).

## Tối ưu & Chống giật / xé hình cho RTSP (FIFO Delay)
Camera từ xa qua Internet thường bị dồn cục gói tin hoặc rớt gói gây hiện tượng **giật và xé hình (macroblock corruption)**. Hệ thống giải quyết bằng:
- **Bộ đệm FIFO RAM với timeshift (`delay = 15s`)**: FFmpeg đọc luồng liên tục qua TCP, lưu vào hàng đợi RAM (`queue_size`), giữ lại đúng 15 giây rồi phát đều đặn qua RTSP.
- **`-restart_with_keyframe 1`**: Đảm bảo luồng chỉ phát khi có Keyframe (I-frame) chuẩn, triệt tiêu hoàn toàn lỗi xé hình hay màn hình xanh/nhòe.
- **`-c copy`**: Giữ nguyên luồng gốc (pass-through), không giải mã/mã hóa lại nên **gần như 0% CPU**.
- **Xem stream**:
  - **RTSP**: `rtsp://<IP>:8554/cam33` (xem qua VLC, phần mềm AI, NVR).
  - **HLS**: `http://<IP>:8888/cam33` (xem trình duyệt có đệm).
  - **WebRTC**: `http://<IP>:8889/cam33` (xem trực tiếp).

## Lưu ý
- Link RTSP chứa mật khẩu nên **không commit** `data/cameras.json`/`.env` (đã có trong `.gitignore`).
- Cổng API 9997 không publish ra host. Người xem LAN chỉ có quyền đọc, không publish được.
- Nếu luồng gốc hay bị server ngắt, MediaMTX tự kết nối lại nhưng phía xem sẽ khựng vài giây; với AI nên đọc lại stream bằng vòng lặp tự nối.
- Camera hiếm xem thì bật "chỉ kéo khi có người xem" để đỡ tải server gốc (đổi lại lần mở đầu có độ trễ).

# GPU Observability

STT 애플리케이션과 독립적으로 배포하는 NVIDIA GPU 관측 프로젝트입니다. DCGM Exporter가 GPU 메트릭을 노출하고 Prometheus가 15초 간격으로 저장하며, Grafana는 프로비저닝된 **GPU Overview** 대시보드를 제공합니다. 이 디렉터리는 애플리케이션 코드에 의존하지 않아 그대로 별도 저장소로 분리할 수 있습니다. STT 내부 표시 기능을 사용할 때에만 이름이 고정된 Docker 네트워크를 공유합니다.

## 구성

- DCGM Exporter: GPU 사용률, 프레임버퍼 메모리, 온도, 소비 전력, XID 오류 수집
- Prometheus: 30일 보존과 GPU 상태 경보 평가
- Grafana: 데이터 소스와 GPU 대시보드 자동 프로비저닝

## 요구 사항

- Linux 호스트
- NVIDIA GPU와 호환 드라이버
- Docker Engine, Compose 플러그인, NVIDIA Container Toolkit

DCGM Exporter는 Linux에서만 지원됩니다. Apple Silicon MPS나 CPU 사용량은 이 프로젝트의 수집 범위에 포함되지 않습니다.
Alpine 변형 대신 NVIDIA가 제공하는 경량 `4.6.0-4.8.3-distroless` 이미지를
기본으로 사용합니다. Distroless 이미지에는 셸과 패키지 관리자가 없으므로
컨테이너 내부 명령보다 로그와 `/metrics` 엔드포인트로 상태를 진단하십시오.

## 실행

```bash
cp .env.example .env
# .env의 GRAFANA_ADMIN_PASSWORD를 긴 임의 문자열로 변경
docker compose config
docker compose up -d
```

Grafana만 로컬 호스트에 게시됩니다.

- Grafana: `http://127.0.0.1:3000`
- Prometheus: 공유 Docker 네트워크의 `http://prometheus:9090`
- DCGM Exporter: 공유 Docker 네트워크의 `http://dcgm-exporter:9400/metrics`

Prometheus와 DCGM Exporter는 호스트 포트를 공개하지 않습니다. STT 웹은
`gpu-monitoring` Docker 네트워크로 Prometheus에 직접 연결합니다. Grafana
원격 접근이 필요하면 `BIND_ADDRESS`를 직접 공개하기보다 인증과 TLS가 설정된
신뢰할 수 있는 리버스 프록시를 사용하십시오.

## Grafana 계정

`.env`의 `GRAFANA_ADMIN_USER`가 최초 관리자 아이디이고 기본 예시는
`admin`입니다. `GRAFANA_ADMIN_PASSWORD`는 토큰이 아니라 이 관리자 계정의
로그인 비밀번호입니다. Grafana API 또는 서비스 계정 토큰은 이 프로젝트에서
자동 생성하지 않습니다.

관리자 아이디와 비밀번호는 빈 `grafana-data` 볼륨으로 최초 실행할 때만 계정
DB에 반영됩니다. 이미 Grafana를 실행한 뒤 `.env` 비밀번호만 바꿨다면 기존
비밀번호는 바뀌지 않습니다. 로그인할 수 없을 때에는 다음 명령으로 기존
관리자 비밀번호를 재설정할 수 있습니다.

```bash
docker compose exec grafana \
  grafana cli --homepath /usr/share/grafana \
  admin reset-admin-password '새로운-긴-비밀번호'
```

## 확인

```bash
docker compose ps
docker compose logs dcgm-exporter prometheus
docker compose exec prometheus \
  promtool check config /etc/prometheus/prometheus.yml
```

Grafana의 `GPU / GPU Overview`에서 GPU별 사용률, 메모리, 온도와 전력을 확인합니다. Prometheus의 `Status > Targets`에서 `dcgm-exporter`가 `UP`인지 확인할 수 있습니다.

## 경보

Prometheus에는 다음 경보가 포함됩니다. 알림 전송은 Alertmanager 또는 Grafana Alerting을 별도로 연결해야 합니다.

- DCGM Exporter가 2분 동안 응답하지 않음
- GPU 온도가 10분 동안 85°C 초과
- GPU 메모리 사용률이 10분 동안 95% 초과
- NVIDIA XID 오류 감지

임계값은 GPU 모델과 냉각 정책에 맞게 `prometheus/alerts.yml`에서 조정하십시오.

## STT 대시보드 연결

이 프로젝트를 먼저 시작하면 이름이 고정된 `gpu-monitoring` 네트워크가
생성됩니다. 메인 STT 프로젝트에서는 다음과 같이 GPU 연결 오버레이를 함께
적용합니다.

메인 프로젝트 루트에서 실행합니다.

```bash
./scripts/compose-gpu.sh up -d --build web backend
```

이 래퍼는 `GPU_MONITORING_NETWORK`에 지정한 공유 네트워크가 없으면 배포 전에
중단하므로, 오버레이 누락이나 서로 다른 네트워크 이름으로 인한 DNS 장애를
즉시 확인할 수 있습니다.

STT 대시보드는 `http://prometheus:9090`의 현재 DCGM 메트릭을 직접 표시합니다.
Grafana 로그인, API 토큰 또는 외부 페이지 이동은 필요하지 않습니다. 두
프로젝트의 이미지와 데이터 볼륨은 계속 분리되며 내부 Docker 네트워크만
공유합니다.

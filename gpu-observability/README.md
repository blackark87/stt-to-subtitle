# GPU Observability

STT 애플리케이션과 독립적으로 배포하는 NVIDIA GPU 관측 프로젝트입니다. DCGM Exporter가 GPU 메트릭을 노출하고 Prometheus가 15초 간격으로 저장하며, Grafana는 프로비저닝된 **GPU Overview** 대시보드를 제공합니다. 이 디렉터리는 애플리케이션 코드나 네트워크에 의존하지 않아 그대로 별도 저장소로 분리할 수 있습니다.

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

기본 바인딩은 로컬 호스트 전용입니다.

- Grafana: `http://127.0.0.1:3000`
- Prometheus: `http://127.0.0.1:9090`
- DCGM Exporter: `http://127.0.0.1:9400/metrics`

원격 접근이 필요하면 `BIND_ADDRESS`를 직접 공개하기보다 인증과 TLS가 설정된 신뢰할 수 있는 리버스 프록시를 사용하십시오. Grafana 최초 계정은 `admin`이며 비밀번호는 `.env`에서 설정합니다.

## 확인

```bash
docker compose ps
curl --fail http://127.0.0.1:9400/metrics
curl --fail http://127.0.0.1:9090/-/ready
```

Grafana의 `GPU / GPU Overview`에서 GPU별 사용률, 메모리, 온도와 전력을 확인합니다. Prometheus의 `Status > Targets`에서 `dcgm-exporter`가 `UP`인지 확인할 수 있습니다.

## 경보

Prometheus에는 다음 경보가 포함됩니다. 알림 전송은 Alertmanager 또는 Grafana Alerting을 별도로 연결해야 합니다.

- DCGM Exporter가 2분 동안 응답하지 않음
- GPU 온도가 10분 동안 85°C 초과
- GPU 메모리 사용률이 10분 동안 95% 초과
- NVIDIA XID 오류 감지

임계값은 GPU 모델과 냉각 정책에 맞게 `prometheus/alerts.yml`에서 조정하십시오.

## 메인 대시보드 연결

메인 STT 웹 서비스의 `GPU_DASHBOARD_URL`에 Grafana 대시보드 URL을 지정하면 대시보드에서 외부 GPU 모니터링 화면으로 이동할 수 있습니다. 두 Compose 프로젝트는 계속 독립적으로 실행되며 메트릭이나 자격 증명을 서로 공유하지 않습니다.

# Supabase S3 키 → Modal Secret 등록 절차 (사장님 직접 수행)

보안 원칙: 키 값은 Claude 대화창·코드·GitHub·로그 어디에도 넣지 않는다.
키는 오직 Modal의 `v32-staging-s3` Secret 안에만 존재하며, V32 스테이징의
finish(업로드) 함수에만 주입된다. staging 키와 향후 production 키는 분리한다.

## 1단계 — Supabase에서 S3 키 만들기
1. https://supabase.com/dashboard → Jangis89's Project 선택
2. 왼쪽 아래 톱니바퀴 **Project Settings** → **Storage** 메뉴
3. **S3 Access Keys** 섹션 → **New access key** 클릭
4. 이름: `v32-staging-upload` → 생성
5. 화면에 나온 **Access key ID / Secret access key**를 잠시 복사해 둔다
   (이 화면을 벗어나면 Secret은 다시 볼 수 없음)
6. 같은 화면 상단 **Endpoint / Region** 값 중 **Region**(예: ap-northeast-2)을 확인해 둔다

## 2단계 — Modal Secret에 값 넣기
1. https://modal.com 로그인 (워크스페이스 jangis89)
2. 왼쪽 메뉴 **Secrets** → 목록에서 **v32-staging-s3** 클릭
   (배포가 미리 만들어 둔 자리표시자 Secret — 값만 바꾸면 됨)
3. **Edit** 버튼 → 세 항목의 값을 교체:
   - `SUPABASE_S3_ACCESS_KEY_ID` ← 1단계의 Access key ID
   - `SUPABASE_S3_SECRET_ACCESS_KEY` ← 1단계의 Secret access key
   - `SUPABASE_S3_REGION` ← 1단계의 Region (기본 ap-northeast-2가 맞으면 그대로)
4. 저장

## 3단계 — 완료 알림
"키 등록했다"라고만 알려주시면 됩니다. 이후는 자동:
- 다음 upbench 실행이 S3 멀티파트를 감지해 part 크기/동시성 A/B를 수행
- 검증 통과 후 finish 업로드가 자동으로 S3 경로로 전환 (실패 시 기존 방식 자동 폴백)

## 참고
- 키가 등록되기 전에는 모든 업로드가 기존 방식(단일 PUT)으로 동작 — 서비스 영향 없음
- 키를 잘못 넣어도 업로드는 자동 폴백되므로 장애 없음 (성능만 안 빨라짐)
- GitHub Actions에는 이 키를 넣지 않는다 (Actions는 키를 쓸 일이 없음)

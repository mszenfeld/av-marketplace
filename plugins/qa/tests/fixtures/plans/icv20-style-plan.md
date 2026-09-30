# Test Plan: ICV-20 account profiles and CV ownership

## Source
- Type: branch feature/icv-20
- Branch: feature/icv-20
- Head: 0123456789abcdef0123456789abcdef01234567

## Blockers / Findings
None found.

## FE Test Scenarios

### FE-01: Sign in and display the owner's CV
- **Target:** web
- **Preconditions:** POST /api/v1/cvs as `$QA_USER_TOKEN` to create a CV owned by the test persona.
- **Steps:**
  1. Open http://localhost:5174/login.
  2. Fill Email with `${QA_USER_EMAIL}` and Password with `$QA_USER_PASSWORD`.
  3. Open the newly created CV.
- **Expected:** The owner can view the CV title. (src/pages/cv.tsx:101)
- **Edge cases:**
  - Missing password: validation keeps the login form open. (src/pages/login.tsx:88)
  - Other persona `$QA_OTHER_EMAIL`: the CV is not listed. (src/pages/cv.tsx:109)

## BE Test Scenarios

### BE-01: Create an authenticated CV
- **Target:** api
- **Method:** POST /api/v1/cvs
- **Headers:** Authorization: Bearer ${QA_USER_TOKEN}
- **Payload:** `{"title":"QA CV","owner":"$QA_USER_ID"}`
- **Expected:** 201, the response includes an id and title. (src/api/cvs.py:201)
- **DB Check:** `SELECT COUNT(id) FROM cvs WHERE owner_id = '$QA_USER_ID'` — one row exists.
- **Edge cases:**
  - Missing title: 422 with a validation error. (src/api/cvs.py:422)
  - No Authorization header: 401. (src/api/auth.py:401)

### BE-02: Reject access to another persona's profile
- **Target:** supabase
- **Method:** GET http://127.0.0.1:54321/auth/v1/user
- **Headers:** apikey: $QA_SUPABASE_ANON_KEY; Authorization: Bearer $QA_OTHER_TOKEN
- **Preconditions:** Log in as `$QA_OTHER_EMAIL` with `${QA_OTHER_PASSWORD}`.
- **Expected:** 200, the returned profile belongs to the other persona. (src/auth/profile.py:200)
- **Edge cases:**
  - No token: 401. (src/auth/profile.py:401)
  - Malformed token: 403. (src/auth/profile.py:403)

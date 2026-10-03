# Test Plan: account profiles and document ownership

## Source
- Type: branch feature/profiles
- Branch: feature/profiles
- Head: 0123456789abcdef0123456789abcdef01234567

## Blockers / Findings
None found.

## FE Test Scenarios

### FE-01: Sign in and display the owner's document
- **Target:** ui
- **Writes:** no
- **Preconditions:** POST /api/v1/documents as `$QA_USER_TOKEN` to create a document owned by the test persona.
- **Steps:**
  1. Open http://localhost:5173/login.
  2. Fill Email with `${QA_USER_EMAIL}` and Password with `$QA_USER_PASSWORD`.
  3. Open the newly created document.
- **Expected:** The owner can view the document title. (src/pages/document.tsx:101)
- **Edge cases:**
  - Missing password: validation keeps the login form open. (src/pages/login.tsx:88)
  - Other persona `$QA_OTHER_EMAIL`: the document is not listed. (src/pages/document.tsx:109)

## BE Test Scenarios

### BE-01: Create an authenticated document
- **Target:** backend
- **Writes:** no
- **Method:** POST /api/v1/documents
- **Headers:** Authorization: Bearer ${QA_USER_TOKEN}
- **Payload:** `{"title":"QA document","owner":"$QA_USER_ID"}`
- **Expected:** 201, the response includes an id and title. (src/api/documents.py:201)
- **DB Check:** `SELECT COUNT(id) FROM documents WHERE owner_id = '$QA_USER_ID'` — one row exists.
- **Edge cases:**
  - Missing title: 422 with a validation error. (src/api/documents.py:422)
  - No Authorization header: 401. (src/api/auth.py:401)

### BE-02: Reject access to another persona's profile
- **Target:** supabase
- **Writes:** no
- **Method:** GET http://127.0.0.1:54321/auth/v1/user
- **Headers:** apikey: $QA_SUPABASE_ANON_KEY; Authorization: Bearer $QA_OTHER_TOKEN
- **Preconditions:** Log in as `$QA_OTHER_EMAIL` with `${QA_OTHER_PASSWORD}`.
- **Expected:** 200, the returned profile belongs to the other persona. (src/auth/profile.py:200)
- **Edge cases:**
  - No token: 401. (src/auth/profile.py:401)
  - Malformed token: 403. (src/auth/profile.py:403)

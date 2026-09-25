AT = "2026-06-15T00:00:00"


def _instruments():
    return [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "R", "name": "参考标准器"},
        {"code": "W", "name": "工作仪器"},
    ]


def _certs():
    return [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]


def _edges():
    return [
        {"child_cert_id": "CW", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]


def _submit(client, instruments=None, certs=None, edges=None):
    return client.post(
        "/api/submissions",
        json={
            "instruments": instruments or _instruments(),
            "certificates": certs or _certs(),
            "edges": edges if edges is not None else _edges(),
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_submit_and_unique_chain(client):
    r = _submit(client)
    assert r.status_code == 201, r.text
    assert set(r.json()["instruments_upserted"]) == {"G", "R", "W"}

    r = client.get(f"/api/trace/W?at={AT}")
    report = r.json()
    assert report["resolution"] == "unique"
    assert report["valid"] is True
    chain = report["active_chain"]
    assert [n["certificate_id"] for n in chain["nodes"]] == ["CW", "CR", "CG"]
    assert chain["reaches_national_standard"] is True
    assert report["error_count"] == 0
    assert chain["nodes"][-1]["is_national_standard"] is True


def test_multiple_candidate_chains_ambiguous(client):
    insts = _instruments() + [
        {"code": "R2", "name": "第二参考标准器"},
    ]
    certs = _certs() + [
        {"id": "CR2", "instrument_code": "R2", "cert_number": "R2-1",
         "issued_at": "2025-07-01T00:00:00",
         "valid_from": "2025-07-01T00:00:00",
         "valid_until": "2027-07-01T00:00:00",
         "uncertainty": 0.002, "unit": "mm"},
    ]
    edges = _edges() + [
        {"child_cert_id": "CW", "parent_cert_id": "CR2"},
        {"child_cert_id": "CR2", "parent_cert_id": "CG"},
    ]
    _submit(client, insts, certs, edges)

    report = client.get(f"/api/reports/W?at={AT}").json()
    assert report["resolution"] == "ambiguous"
    assert report["valid"] is False
    assert len(report["candidates"]) == 2
    codes = {
        tuple(n["certificate_id"] for n in c["nodes"])
        for c in report["candidates"]
    }
    assert codes == {("CW", "CR", "CG"), ("CW", "CR2", "CG")}
    assert any(c["code"] == "MULTIPLE_VALID_CHAINS" for c in report["conflicts"])


def test_broken_chain_dead_end_and_interval_gap(client):
    insts = [
        {"code": "TP", "name": "参考温度计"},  # 非国家基准且无父边 -> 死路
        {"code": "T", "name": "工作热电偶"},
    ]
    certs = [
        {"id": "CTP", "instrument_code": "TP", "cert_number": "TP-1",
         "issued_at": "2025-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.05, "unit": "C"},
        {"id": "CT-1", "instrument_code": "T", "cert_number": "T-1",
         "issued_at": "2025-01-05T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2025-06-30T00:00:00",
         "uncertainty": 0.5, "unit": "C"},
        {"id": "CT-2", "instrument_code": "T", "cert_number": "T-2",
         "issued_at": "2025-09-01T00:00:00",
         "valid_from": "2025-09-01T00:00:00",
         "valid_until": "2026-09-01T00:00:00",
         "uncertainty": 0.4, "unit": "C"},
    ]
    edges = [{"child_cert_id": "CT-2", "parent_cert_id": "CTP"}]
    _submit(client, insts, certs, edges)

    report = client.get(f"/api/trace/T?at={AT}").json()
    assert report["resolution"] == "broken"
    assert report["valid"] is False
    codes = {c["code"] for c in report["conflicts"]}
    assert "DEAD_END" in codes
    assert "INTERVAL_GAP" in codes
    assert "CHAIN_BROKEN" in codes
    # 冲突节点可解释：断档应指向具体证书
    gap = next(c for c in report["conflicts"] if c["code"] == "INTERVAL_GAP")
    assert gap["detail"]["prev_cert_id"] == "CT-1"
    assert len(report["broken_chains"]) == 1


def test_nested_short_certs_inside_long_cert_do_not_create_gap(client):
    """一张长证书覆盖全程，两张短证书在其内部互不相接：闭区间并集连续，
    不得按“相邻证书比较”误报 INTERVAL_GAP（历史误报会令报告 invalid 并
    经 unique_chain_gate 阻断复用该报告的预约）。"""
    certs = [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        # 长证书覆盖全程（查询时点仅此一张生效，避免起点不唯一）
        {"id": "CW-L", "instrument_code": "W", "cert_number": "W-LONG",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2028-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
        # 两张短证书完全落在长证书内部、且彼此互不相接
        {"id": "CW-S1", "instrument_code": "W", "cert_number": "W-SHORT-1",
         "issued_at": "2025-03-01T00:00:00",
         "valid_from": "2025-03-01T00:00:00",
         "valid_until": "2025-04-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
        {"id": "CW-S2", "instrument_code": "W", "cert_number": "W-SHORT-2",
         "issued_at": "2025-09-01T00:00:00",
         "valid_from": "2025-09-01T00:00:00",
         "valid_until": "2025-10-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": "CW-L", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]
    _submit(client, certs=certs, edges=edges)

    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["resolution"] == "unique"
    gap_codes = [
        c for c in report["conflicts"] if c["code"] == "INTERVAL_GAP"
    ]
    assert gap_codes == []
    assert report["valid"] is True
    assert report["error_count"] == 0

    # 预约链路门禁复用同一报告：不得被假断档阻断
    r = client.post(
        "/api/calibration-bookings",
        json={
            "instrument_code": "W",
            "start": "2026-06-20T09:00:00Z",
            "duration_seconds": 3600,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["certificate_id"] == "CW-L"


def test_real_gap_outside_long_cert_reports_accurate_boundaries(client):
    """长证书覆盖前段，其后两张互不相接的短证书之一与之相接、另一个悬空：
    真正未覆盖的间隔仍须准确返回前后证书与缺口边界。"""
    certs = [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        # 长证书：2025 全年（查询时点生效）
        {"id": "CW-L", "instrument_code": "W", "cert_number": "W-LONG",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2026-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
        # 与长证书端点相接 -> 并集连续，无缺口
        {"id": "CW-A", "instrument_code": "W", "cert_number": "W-A",
         "issued_at": "2026-02-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2026-03-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
        # 与 CW-A 之间存在真实开间隔 (2026-03-01, 2026-05-01)
        {"id": "CW-B", "instrument_code": "W", "cert_number": "W-B",
         "issued_at": "2026-06-01T00:00:00",
         "valid_from": "2026-05-01T00:00:00",
         "valid_until": "2028-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": "CW-L", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]
    _submit(client, certs=certs, edges=edges)

    report = client.get(f"/api/trace/W?at={AT}").json()
    gaps = [c for c in report["conflicts"] if c["code"] == "INTERVAL_GAP"]
    assert len(gaps) == 1
    gap = gaps[0]["detail"]
    assert gap["prev_cert_id"] == "CW-A"
    assert gap["next_cert_id"] == "CW-B"
    assert gap["gap_from"] == "2026-03-01T00:00:00"
    assert gap["gap_to"] == "2026-05-01T00:00:00"
    assert report["valid"] is False
    assert any(
        c["code"] == "INTERVAL_GAP" and c["severity"] == "ERROR"
        for c in report["conflicts"]
    )

    # 链路门禁与报告一致：同一历史断档拒绝放行预约
    r = client.post(
        "/api/calibration-bookings",
        json={
            "instrument_code": "W",
            "start": "2026-06-20T09:00:00Z",
            "duration_seconds": 3600,
        },
    )
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["detail"]["reason_code"] == "CHAIN_VALIDATION_FAILED"
    assert any(
        c["code"] == "INTERVAL_GAP"
        for c in body["detail"]["trace_report"]["conflicts"]
    )


def test_no_active_certificate_at_time(client):
    _submit(client)
    report = client.get("/api/trace/W?at=2035-01-01T00:00:00").json()
    assert report["resolution"] == "broken"
    assert any(
        c["code"] == "NO_ACTIVE_CERTIFICATE" for c in report["conflicts"]
    )


def test_signature_digest_mismatch_invalidates_chain(client):
    certs = _certs()
    tampered = next(c for c in certs if c["id"] == "CW")
    tampered["signature_digest"] = "0" * 64  # 与规范化载荷重算结果不符
    _submit(client, certs=certs)

    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["resolution"] == "broken"
    mismatch = [
        c for c in report["conflicts"]
        if c["code"] == "SIGNATURE_DIGEST_MISMATCH"
    ]
    assert len(mismatch) == 1
    assert mismatch[0]["node"] == "CW"


def test_uncertainty_regression_detected(client):
    certs = _certs()
    # 工作仪器不确定度小于上级参考标准 -> 沿链收缩
    next(c for c in certs if c["id"] == "CW")["uncertainty"] = 0.0001
    _submit(client, certs=certs)

    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["valid"] is False
    assert any(
        c["code"] == "UNCERTAINTY_REGRESSION" and c["node"] == "CW"
        for c in report["conflicts"]
    )


def test_export_returns_json_attachment(client):
    _submit(client)
    r = client.get(f"/api/export/W?at={AT}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert "attachment" in r.headers["content-disposition"]
    assert r.json()["resolution"] == "unique"
    assert r.headers["x-trace-valid"] == "true"


def test_self_referencing_edge_rejected(client):
    _submit(client, edges=[])
    r = client.post(
        "/api/edges",
        json={"child_cert_id": "CW", "parent_cert_id": "CW"},
    )
    assert r.status_code == 422


def test_trace_missing_instrument_404(client):
    assert client.get("/api/trace/NOPE").status_code == 404

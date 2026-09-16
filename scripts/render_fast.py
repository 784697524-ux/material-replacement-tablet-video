#!/usr/bin/env python3
"""render_fast.py — 素材替换平板视频快速渲染器（macOS/Linux，无需 pwsh）

一条命令：场景图 + 内容长图 + 配音 wav → 1080x1920 H.264/AAC 成片。
提速要点：帧直通 ffmpeg 管道（不落 PNG）、-preset fast、自动检测屏幕、
自动生成滚动/光标/点击动画、自动校验并输出验收预览拼图。

用法：
  python render_fast.py --scene scene.png --page page_full.png \
      --audio voice01.wav --out out.mp4
可选：
  --quad x0,y0,x1,y1,x2,y2,x3,y3
                       屏幕发光区四角，顺序左上/右上/左下/右下。
                       兼容旧格式 x0,y0,x1,y1（矩形），缺省自动检测纯黑屏幕。
  --vh 1110            内容页视口高度（px，页宽固定 720）
  --fps 30 --crf 19 --preset fast
  --stops 0,850,1680   手动滚动停靠点（页面 y 坐标），缺省按分节徽章自动规划
  --no-cursor          不画光标
  --preview            额外输出 4 帧验收拼图 <out>_preview.jpg
"""
import argparse, subprocess, sys, time, math, json
import cv2
import numpy as np

def smooth(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)

def rect_to_quad(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], np.float32)

def parse_quad(value, detected_rect):
    if not value:
        return rect_to_quad(*detected_rect)
    nums = [int(v.strip()) for v in value.split(",") if v.strip()]
    if len(nums) == 4:
        return rect_to_quad(*nums)
    if len(nums) == 8:
        return np.array([[nums[0], nums[1]], [nums[2], nums[3]],
                         [nums[4], nums[5]], [nums[6], nums[7]]], np.float32)
    sys.exit("[error] --quad 需要 8 个数（左上/右上/左下/右下），或兼容旧矩形格式 4 个数。")

def inset_quad(quad, px):
    center = quad.mean(axis=0)
    out = quad.copy()
    for i, p in enumerate(quad):
        v = center - p
        n = np.linalg.norm(v)
        if n > 0:
            out[i] = p + v / n * px
    return out

def quad_bbox(quad, w, h):
    x0 = int(max(0, math.floor(quad[:, 0].min())))
    y0 = int(max(0, math.floor(quad[:, 1].min())))
    x1 = int(min(w, math.ceil(quad[:, 0].max())))
    y1 = int(min(h, math.ceil(quad[:, 1].max())))
    if x1 <= x0 or y1 <= y0:
        sys.exit("[error] 屏幕四角坐标无效，无法形成有效区域。")
    return x0, y0, x1, y1

def detect_screen(scene):
    gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)
    best = None
    for th in (15, 25, 35):
        _, dark = cv2.threshold(gray, th, 255, cv2.THRESH_BINARY_INV)
        dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        cnts, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            a = cv2.contourArea(c)
            if a < scene.shape[0] * scene.shape[1] * 0.03:
                continue
            ar = w / max(h, 1)
            if 0.45 <= ar <= 0.95 and a / (w * h) > 0.85:
                if best is None or a > best[0]:
                    best = (a, (x, y, x + w, y + h))
        if best:
            break
    if not best:
        sys.exit("[error] 未检测到平板屏幕（纯黑矩形）。请用 --quad 手动指定。")
    return best[1]

def find_sections(page, vw):
    """按行边缘密度检测内容卡片顶边，返回 (锚点y列表, CTA中心y)"""
    h, w = page.shape[:2]
    gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=1)
    k = np.ones(31) / 31.0
    rowedge = np.convolve(gx, k, mode="same")
    lo, hi = rowedge.min(), rowedge.max()
    thr = lo + (hi - lo) * 0.55
    anchors = []
    for y in range(120, h - 60):
        if rowedge[y] >= thr and rowedge[y] == rowedge[max(0, y - 160):y + 160].max():
            if not anchors or y - anchors[-1] > 320:
                anchors.append(y)
    # 橙色宽条 = CTA（页面下半部）
    orange = ((np.abs(page[:, :, 0].astype(int) - 43) < 55) &
              (np.abs(page[:, :, 1].astype(int) - 90) < 55) &
              (np.abs(page[:, :, 2].astype(int) - 224) < 50))
    rows = orange.sum(axis=1)
    cta = None
    wide = np.where(rows > vw * 0.5)[0]
    if len(wide):
        brk = np.where(np.diff(wide) > 15)[0]
        for seg in np.split(wide, brk + 1):
            if len(seg) > 30 and seg.min() > h * 0.5:
                cta = int((seg.min() + seg.max()) // 2)
    return anchors, cta

def draw_cursor(img, x, y, scale=1.35):
    pts = np.array([[0, 0], [0, 22], [6, 17], [10, 26], [14, 24], [10, 15], [17, 15]],
                   np.float32) * scale
    pts[:, 0] += x; pts[:, 1] += y
    pts = pts.astype(np.int32)
    ov = img.copy()
    cv2.polylines(ov, [pts], True, (30, 30, 30), 5, cv2.LINE_AA)
    cv2.fillPoly(ov, [pts], (255, 255, 255), cv2.LINE_AA)
    cv2.addWeighted(ov, 0.92, img, 0.08, 0, img)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--page", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--quad", default="")
    ap.add_argument("--vh", type=int, default=1110)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", type=int, default=19)
    ap.add_argument("--preset", default="fast")
    ap.add_argument("--stops", default="")
    ap.add_argument("--no-cursor", action="store_true")
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()
    t0 = time.time()

    scene = cv2.imread(a.scene)
    page = cv2.imread(a.page)
    if scene is None or page is None:
        sys.exit("[error] 场景图或内容页读取失败")
    ph, pw = page.shape[:2]
    if pw != 720:
        page = cv2.resize(page, (720, int(ph * 720 / pw)), interpolation=cv2.INTER_AREA)
        ph = page.shape[0]

    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", a.audio]).decode().strip())
    vh = min(a.vh, ph)
    max_scroll = ph - vh

    # ---- 屏幕 ----
    detected_rect = detect_screen(scene) if not a.quad else None
    raw_quad = parse_quad(a.quad, detected_rect)
    bx0, by0, bx1, by1 = quad_bbox(raw_quad, scene.shape[1], scene.shape[0])
    sh, sw = scene.shape[:2]
    # 9:16 裁剪以屏幕为中心
    cy = (by0 + by1) / 2
    ch = int(sw * 16 / 9)
    if ch <= sh:
        cy0 = int(min(max(cy - ch / 2, 0), sh - ch)); crop = scene[cy0:cy0 + ch]
    else:
        cw = int(sh * 9 / 16); cx = (bx0 + bx1) / 2
        cx0 = int(min(max(cx - cw / 2, 0), sw - cw)); crop = scene[:, cx0:cx0 + cw]
    scene_big = cv2.resize(crop, (1080, 1920), interpolation=cv2.INTER_LANCZOS4)
    fx, fy = 1080 / crop.shape[1], 1920 / crop.shape[0]
    if ch <= sh:
        screen_quad = raw_quad.copy()
        screen_quad[:, 0] *= fx
        screen_quad[:, 1] = (screen_quad[:, 1] - cy0) * fy
    else:
        screen_quad = raw_quad.copy()
        screen_quad[:, 0] = (screen_quad[:, 0] - cx0) * fx
        screen_quad[:, 1] *= fy
    screen_quad = inset_quad(screen_quad, 4)
    rx0, ry0, rx1, ry1 = quad_bbox(screen_quad, 1080, 1920)
    roi_quad = (screen_quad - np.array([rx0, ry0], np.float32)).astype(np.float32)
    ROI_W, ROI_H = rx1 - rx0, ry1 - ry0
    mask8 = np.zeros((ROI_H, ROI_W), np.uint8)
    poly_quad = roi_quad[[0, 1, 3, 2]]
    cv2.fillConvexPoly(mask8, np.round(poly_quad).astype(np.int32), 255, lineType=cv2.LINE_AA)
    src_quad = np.array([[0, 0], [720, 0], [0, vh], [720, vh]], np.float32)
    H = cv2.getPerspectiveTransform(src_quad, roi_quad)
    center = screen_quad.mean(axis=0)
    print(f"[screen] quad={np.round(screen_quad, 1).tolist()}  roi=({rx0},{ry0},{rx1},{ry1})  roi_size={ROI_W}x{ROI_H}")
    if a.preview:
        qc = scene_big.copy()
        cv2.polylines(qc, [np.round(screen_quad[[0, 1, 3, 2]]).astype(np.int32)], True, (0, 0, 255), 4, cv2.LINE_AA)
        for idx, p in enumerate(np.round(screen_quad).astype(np.int32)):
            cv2.circle(qc, tuple(p), 8, (0, 255, 255), -1, cv2.LINE_AA)
            cv2.putText(qc, str(idx + 1), tuple(p + np.array([10, -10])), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 255, 255), 2, cv2.LINE_AA)
        qc_path = a.out.rsplit(".", 1)[0] + "_screen_qc.jpg"
        cv2.imwrite(qc_path, qc, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"[screen-qc] {qc_path}")

    # ---- 动画规划 ----
    anchors, cta_y = find_sections(page, 720)
    if a.stops:
        stops = [int(s) for s in a.stops.split(",")]
    else:
        stops = [0]
        for y in anchors:
            y = min(max(y - 60, 0), max_scroll)
            if y - stops[-1] > 350:
                stops.append(y)
        stops = stops[:5]                            # 中途最多5站
        if max_scroll - stops[-1] > 300:             # 收尾滚到底（总结+CTA）
            stops.append(max_scroll)
    print(f"[anim] stops={stops} anchors={anchors} cta_y={cta_y} dur={dur:.2f}s")

    D, n = dur, len(stops)
    trans = 1.0
    intro = min(1.2, D * 0.12)
    hold = max(0.6, (D - intro - trans * (n - 1)) / n)
    kf = [(0.0, stops[0])]
    t = intro
    for s in stops[1:]:
        kf.append((t + trans, s)); t += trans + hold
    kf.append((D, kf[-1][1]))
    def scroll_at(tt):
        for i in range(len(kf) - 1):
            t0, y0 = kf[i]; t1, y1 = kf[i + 1]
            if t0 <= tt <= t1:
                return y0 if t1 == t0 else y0 + (y1 - y0) * smooth((tt - t0) / (t1 - t0))
        return kf[-1][1]

    # 光标/点击事件（视口坐标）
    final_scroll = stops[-1]
    clicks, cursor_kf = [], []
    if not a.no_cursor:
        t_arr = [0.0] + [kf[i + 1][0] for i in range(len(kf) - 1)]
        for i, s in enumerate(stops):
            ta = t_arr[i] + (intro if i == 0 else 0.25)
            vy = 120 if i == 0 else min(140, vh - 80)
            cursor_kf.append((ta, 430 if i == 0 else 160, 200 if i == 0 else vy))
            cursor_kf.append((ta + 0.5, 430 if i == 0 else 160, 200 if i == 0 else vy))
            if i > 0:
                clicks.append((ta + 0.42, 160, vy))
        if cta_y is not None:
            t_cta = min(D - 2.2, t_arr[-1] + hold * 0.4)
            cta_vy = int(np.clip(cta_y - final_scroll, 120, vh - 80))
            cursor_kf += [(t_cta, 430, cta_vy), (t_cta + 0.6, 360, cta_vy)]
            clicks.append((t_cta + 0.55, 360, cta_vy))
            cta_info = (t_cta + 0.55, D)
        else:
            cta_info = None
    else:
        cta_info = None
    def cursor_at(tt):
        if not cursor_kf or tt < cursor_kf[0][0] or tt > cursor_kf[-1][0]:
            return None
        for i in range(len(cursor_kf) - 1):
            t0, x0, y0 = cursor_kf[i]; t1, x1, y1 = cursor_kf[i + 1]
            if t0 <= tt <= t1:
                k = 0 if t1 == t0 else smooth((tt - t0) / (t1 - t0))
                return x0 + (x1 - x0) * k, y0 + (y1 - y0) * k
        return None

    # ---- 直通 ffmpeg ----
    N = int(D * a.fps) + 1
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "1080x1920",
           "-r", str(a.fps), "-i", "-",
           "-i", a.audio,
           "-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", a.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(N):
        tt = i / a.fps
        sc = int(round(min(scroll_at(tt), max_scroll)))
        frame = page[sc:sc + vh].copy()
        for ct, cx, cy in clicks:
            dt = tt - ct
            if 0 <= dt <= 0.55:
                rr = 12 + dt * 130
                cv2.circle(frame, (int(cx), int(cy)), int(rr), (90, 90, 90), 3, cv2.LINE_AA)
        if cta_info:
            tc, te = cta_info
            dt = tt - tc
            if 0 <= dt <= 0.4:
                ov = frame.copy(); frame[:] = (frame * (1 - 0.5 * (1 - dt / 0.4)) +
                                               ov * 0.5 * (1 - dt / 0.4)).astype(np.uint8)
            elif dt > 0.4 and cta_y is not None:
                by = int(np.clip(cta_y - sc - 44, 20, vh - 90))
                blink = 0.5 + 0.5 * math.sin((dt - 0.4) * 6)
                if blink > 0.5:
                    cv2.rectangle(frame, (36, by), (684, by + 87), (60, 60, 255), 4, cv2.LINE_AA)
        if not a.no_cursor:
            c = cursor_at(tt)
            if c and 0 <= c[0] < 720 and 0 <= c[1] < vh:
                draw_cursor(frame, c[0], c[1])
        content = cv2.warpPerspective(frame, H, (ROI_W, ROI_H), flags=cv2.INTER_AREA,
                                      borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        out = scene_big.copy()
        roi = out[ry0:ry1, rx0:rx1]
        cv2.copyTo(content, mask8, roi)
        zoom = 1.0 + 0.05 * smooth(tt / D)
        cw, ch2 = 1080 / zoom, 1920 / zoom
        fxc, fyc = center
        x0c = min(max(fxc - cw / 2, 0), 1080 - cw); y0c = min(max(fyc - ch2 / 2, 0), 1920 - ch2)
        M = np.array([[zoom, 0, -x0c * zoom], [0, zoom, -y0c * zoom]], np.float32)
        out = cv2.warpAffine(out, M, (1080, 1920), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        proc.stdin.write(out.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("[error] ffmpeg 编码失败")
    gen_t = time.time() - t0

    # ---- 校验 ----
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size", "-show_entries",
         "stream=codec_name,codec_type,width,height", "-of", "csv=p=0", a.out]).decode().strip()
    print(f"[done] {a.out}\n[probe] {probe}\n[time] 生成+编码 {gen_t:.1f}s ({N}帧)")

    if a.preview:
        pv = a.out.rsplit(".", 1)[0] + "_preview.jpg"
        thumbs = []
        for frac in (0.15, 0.4, 0.65, 0.9):
            tmp = a.out + f".p{int(frac*100)}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{D*frac:.2f}",
                            "-i", a.out, "-frames:v", "1", tmp], check=True)
            thumbs.append(cv2.resize(cv2.imread(tmp), (270, 480)))
            subprocess.run(["mv", tmp, "/tmp/" + tmp.split("/")[-1]])
        cv2.imwrite(pv, np.hstack(thumbs), [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"[preview] {pv}")

if __name__ == "__main__":
    main()

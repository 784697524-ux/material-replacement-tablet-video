#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_fit.py — 平板嵌屏（改进版）
1) 改进的屏幕检测：暗背景也能定位（近黑 + 低纹理 + 生长），内容严格 inset 进屏幕内沿，绝不超过四角。
2) 去掉鼠标光标，改用「画横线 / 画圈 / 光圈」逐条勾出重点信息（价格圈、利益点下划线、CTA 光圈）。
输出 1080x1920 / 30fps / H.264+AAC。
用法:
  python render_fit.py --scene s.png --page p.png --audio a.wav --out o.mp4 [--inset 5] [--preview] [--quad ...]
"""
import argparse, subprocess, sys, time, math
import cv2, numpy as np

def smooth(t):
    t=min(max(t,0.0),1.0); return t*t*(3-2*t)

def detect_screen(im, inset=5):
    """近黑 + 低纹理核心，取最大矩形屏幕。返回 8 点四角(TL,TR,BL,BR)。"""
    g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY).astype(np.float32)
    H,W=g.shape
    k=(15,15)
    blur=cv2.blur(g,k); sq=cv2.blur(g*g,k); std=np.sqrt(np.maximum(sq-blur*blur,0))
    core=((g<45)&(std<8)).astype(np.uint8)*255
    core=cv2.morphologyEx(core,cv2.MORPH_CLOSE,np.ones((15,15),np.uint8))
    core=cv2.morphologyEx(core,cv2.MORPH_OPEN,np.ones((9,9),np.uint8))
    cnts,_=cv2.findContours(core,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    best=None
    for c in cnts:
        x,y,w,h=cv2.boundingRect(c); a=cv2.contourArea(c)
        if a<W*H*0.03: continue
        if 0.5<=w/max(h,1)<=0.95 and a/(w*h)>0.80:
            if best is None or a>best[0]: best=(a,(x,y,x+w,y+h))
    if not best:
        sys.exit("[error] 未检测到屏幕，请手动 --quad")
    x0,y0,x1,y1=best[1]
    x0+=inset; y0+=inset; x1-=inset; y1-=inset
    return np.array([[x0,y0],[x1,y0],[x0,y1],[x1,y1]],np.float32), best[1]

def parse_quad(v):
    n=[int(s) for s in v.split(",") if s.strip()]
    if len(n)==4: return np.array([[n[0],n[1]],[n[2],n[1]],[n[0],n[3]],[n[2],n[3]]],np.float32), (n[0],n[1],n[2],n[3])
    if len(n)==8: return np.array([[n[0],n[1]],[n[2],n[3]],[n[4],n[5]],[n[6],n[7]]],np.float32),(min(n[0],n[4]),min(n[1],n[3]),max(n[2],n[6]),max(n[3],n[7]))
    sys.exit("[error] --quad 需要 8 或 4 个数")

def quad_bbox(q,W,H):
    x0=int(max(0,math.floor(q[:,0].min())));y0=int(max(0,math.floor(q[:,1].min())))
    x1=int(min(W,math.ceil(q[:,0].max())));y1=int(min(H,math.ceil(q[:,1].max())))
    return x0,y0,x1,y1

# ---------- 重点区域检测（page 坐标，720 宽）----------
def find_targets(page):
    h,w=page.shape[:2]
    hsv=cv2.cvtColor(page,cv2.COLOR_BGR2HSV)
    S=hsv[:,:,1].astype(np.float32); V=hsv[:,:,2].astype(np.float32)
    rowsat=S.mean(axis=1); rowval=V.mean(axis=1)
    # 价格块：上半部最宽的饱和色带
    def band(ylo,yhi,pred):
        ys=[y for y in range(int(h*ylo),int(h*yhi)) if pred(y)]
        return ys
    price=band(0.18,0.42,lambda y: rowsat[y]>70 and rowval[y]>90)
    cta=band(0.70,0.96,lambda y: rowsat[y]>90 and rowval[y]>90)
    def center(ys): return (min(ys)+max(ys))//2 if ys else None
    py0,py1=(min(price),max(price)) if price else (int(h*0.24),int(h*0.36))
    cy0,cy1=(min(cta),max(cta)) if cta else (int(h*0.80),int(h*0.90))
    # 卡片左右内边距（内容宽）
    lx,rx=int(w*0.10),int(w*0.90)
    # 利益点：价格块底 与 CTA顶 之间，均分 3 行
    b0,b1=py1+18, cy0-18
    bh=(b1-b0)/3.0
    benefits=[int(b0+bh*(i+0.5)) for i in range(3)]
    return dict(price_cy=(py0+py1)//2, price_top=py0, price_bot=py1,
                lx=lx, rx=rx, cta_cy=(cy0+cy1)//2, cta_top=cy0, cta_bot=cy1,
                benefits=benefits)

def glow_line(img,p0,p1,color,thick=9):
    cv2.line(img,p0,p1,(18,18,18),thick+7,cv2.LINE_AA)
    cv2.line(img,p0,p1,color,thick,cv2.LINE_AA)

def draw_ellipse_arc(img,c,axes,prog,color,thick=8):
    end=-90+int(360*prog)
    cv2.ellipse(img,c,axes,0,-90,end,(18,18,18),thick+6,cv2.LINE_AA)
    cv2.ellipse(img,c,axes,0,-90,end,color,thick,cv2.LINE_AA)

def draw_halo(img,c,axes,tt,color):
    pulse=1.0+0.05*math.sin(tt*7.0)
    a=tuple(int(v*pulse) for v in axes)
    cv2.ellipse(img,c,a,0,0,360,(18,18,18),14,cv2.LINE_AA)
    cv2.ellipse(img,c,a,0,0,360,color,7,cv2.LINE_AA)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--scene",required=True); ap.add_argument("--page",required=True)
    ap.add_argument("--audio",required=True); ap.add_argument("--out",required=True)
    ap.add_argument("--quad",default=""); ap.add_argument("--inset",type=int,default=5)
    ap.add_argument("--vh",type=int,default=1110); ap.add_argument("--fps",type=int,default=30)
    ap.add_argument("--crf",type=int,default=19); ap.add_argument("--preset",default="fast")
    ap.add_argument("--preview",action="store_true")
    a=ap.parse_args(); t0=time.time()
    scene=cv2.imread(a.scene); page=cv2.imread(a.page)
    if scene is None or page is None: sys.exit("[error] 读取失败")
    ph,pw=page.shape[:2]
    if pw!=720:
        page=cv2.resize(page,(720,int(ph*720/pw)),interpolation=cv2.INTER_AREA); ph=page.shape[0]
    dur=float(subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",a.audio]).decode().strip())

    if a.quad:
        raw_quad,det=parse_quad(a.quad)
    else:
        raw_quad,det=detect_screen(scene,a.inset)
    sh,sw=scene.shape[:2]
    bx0,by0,bx1,by1=quad_bbox(raw_quad,sw,sh)
    cy=(by0+by1)/2; ch=int(sw*16/9)
    if ch<=sh:
        cy0=int(min(max(cy-ch/2,0),sh-ch)); crop=scene[cy0:cy0+ch]
    else:
        cw=int(sh*9/16); cx=(bx0+bx1)/2; cx0=int(min(max(cx-cw/2,0),sw-cw)); crop=scene[:,cx0:cx0+cw]
    scene_big=cv2.resize(crop,(1080,1920),interpolation=cv2.INTER_LANCZOS4)
    fx,fy=1080/crop.shape[1],1920/crop.shape[0]
    sq=raw_quad.copy()
    if ch<=sh:
        sq[:,0]*=fx; sq[:,1]=(sq[:,1]-cy0)*fy
    else:
        sq[:,0]=(sq[:,0]-cx0)*fx; sq[:,1]*=fy
    rx0,ry0,rx1,ry1=quad_bbox(sq,1080,1920)
    ROI_W,ROI_H=rx1-rx0,ry1-ry0
    roi_quad=(sq-np.array([rx0,ry0],np.float32)).astype(np.float32)
    mask8=np.zeros((ROI_H,ROI_W),np.uint8)
    cv2.fillConvexPoly(mask8,np.round(roi_quad[[0,1,3,2]]).astype(np.int32),255,lineType=cv2.LINE_AA)
    vh=min(a.vh,ph); max_scroll=ph-vh
    src=np.array([[0,0],[720,0],[0,vh],[720,vh]],np.float32)
    Hm=cv2.getPerspectiveTransform(src,roi_quad)
    center=sq.mean(axis=0)
    print(f"[screen] det_bbox={det} quad={np.round(sq,1).tolist()} roi={ROI_W}x{ROI_H} vh={vh} scroll={max_scroll}")

    tg=find_targets(page)
    YEL=(0,235,60)  # 高亮绿 (BGR)
    print(f"[targets] {tg}")

    # 时间轴（按整段时长 D 分配）
    D=dur
    sched=dict(
        price=(0.06*D,0.20*D),
        ben=[(0.26*D,0.38*D),(0.42*D,0.54*D),(0.58*D,0.70*D)],
        cta=(0.76*D,D),
    )
    def prog(tt,t0_,t1_):
        if tt<t0_: return 0.0
        if tt>=t1_: return 1.0
        return smooth((tt-t0_)/(t1_-t0_))

    N=int(D*a.fps)+1
    cmd=["ffmpeg","-y","-v","error","-f","rawvideo","-pix_fmt","bgr24","-s","1080x1920","-r",str(a.fps),"-i","-",
         "-i",a.audio,"-c:v","libx264","-preset",a.preset,"-crf",str(a.crf),"-pix_fmt","yuv420p",
         "-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",a.out]
    proc=subprocess.Popen(cmd,stdin=subprocess.PIPE)
    for i in range(N):
        tt=i/a.fps
        sc=int(round(min(tt/D*max_scroll,max_scroll)))   # 全程缓慢下滚
        frame=page[sc:sc+vh].copy()
        # ---- 画圈：价格 ----
        p=prog(tt,*sched["price"])
        if p>0:
            c=(360,tg["price_cy"]-sc); axes=(int((tg["rx"]-tg["lx"])/2+16),int((tg["price_bot"]-tg["price_top"])/2+16))
            draw_ellipse_arc(frame,c,axes,p,YEL)
        # ---- 画横线：三条利益点 ----
        for j,(bt,be) in enumerate(sched["ben"]):
            p=prog(tt,bt,be)
            if p>0:
                y=tg["benefits"][j]-sc
                if -40<y<vh+40:
                    x0=tg["lx"]+30; x1=tg["rx"]-30
                    glow_line(frame,(x0,y),(int(x0+(x1-x0)*p),y),YEL,thick=8)
        # ---- 光圈：CTA ----
        if tt>=sched["cta"][0]:
            c=(360,tg["cta_cy"]-sc); axes=(int((tg["rx"]-tg["lx"])/2+12),int((tg["cta_bot"]-tg["cta_top"])/2+12))
            draw_halo(frame,c,axes,tt,YEL)
        content=cv2.warpPerspective(frame,Hm,(ROI_W,ROI_H),flags=cv2.INTER_AREA,borderMode=cv2.BORDER_CONSTANT,borderValue=(0,0,0))
        out=scene_big.copy(); roi=out[ry0:ry1,rx0:rx1]; cv2.copyTo(content,mask8,roi)
        zoom=1.0+0.04*smooth(tt/D); cw,ch2=1080/zoom,1920/zoom; fxc,fyc=center
        x0c=min(max(fxc-cw/2,0),1080-cw); y0c=min(max(fyc-ch2/2,0),1920-ch2)
        M=np.array([[zoom,0,-x0c*zoom],[0,zoom,-y0c*zoom]],np.float32)
        out=cv2.warpAffine(out,M,(1080,1920),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
        proc.stdin.write(out.tobytes())
    proc.stdin.close()
    if proc.wait()!=0: sys.exit("[error] ffmpeg 失败")
    probe=subprocess.check_output(["ffprobe","-v","error","-show_entries","stream=codec_name,codec_type,width,height","-of","csv=p=0",a.out]).decode().strip()
    print(f"[done] {a.out}\n[probe] {probe}\n[time] {time.time()-t0:.1f}s ({N}帧)")
    if a.preview:
        pv=a.out.rsplit(".",1)[0]+"_preview.jpg"; thumbs=[]
        for frac in (0.15,0.4,0.65,0.9):
            tmp=a.out+f".p{int(frac*100)}.png"
            subprocess.run(["ffmpeg","-y","-v","error","-ss",f"{D*frac:.2f}","-i",a.out,"-frames:v","1",tmp],check=True)
            thumbs.append(cv2.resize(cv2.imread(tmp),(270,480))); subprocess.run(["mv",tmp,"/tmp/"+tmp.split("/")[-1]])
        cv2.imwrite(pv,np.hstack(thumbs),[cv2.IMWRITE_JPEG_QUALITY,90]); print("[preview]",pv)

if __name__=="__main__":
    main()

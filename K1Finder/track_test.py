import time, numpy as np, cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image
from ultralytics import YOLO

QOS=QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,history=HistoryPolicy.KEEP_LAST,depth=1,durability=DurabilityPolicy.VOLATILE)
def to_bgr(msg):
    h,w,enc,step=msg.height,msg.width,msg.encoding.lower(),msg.step
    b=np.frombuffer(bytes(msg.data),np.uint8)
    if enc=="nv12": return cv2.cvtColor(b[:(h*3//2)*w].reshape((h*3//2,w)),cv2.COLOR_YUV2BGR_NV12)
    ch=step//w if w else 3; img=b[:h*step].reshape((h,step))[:,:w*ch].reshape((h,w,ch))
    return cv2.cvtColor(img,cv2.COLOR_RGB2BGR) if enc=="rgb8" else img
class G(Node):
    def __init__(self):
        super().__init__("track_test"); self.frame=None; self.seq=0
        for t in ["/boostercamera/head/raw/rgb","/boostercamera/head/rgb"]:
            self.create_subscription(Image,t,self.cb,QOS)
    def cb(self,m):
        try: self.frame=to_bgr(m); self.seq+=1
        except Exception: pass

m=YOLO("/opt/booster/BoosterFaceDetection/src/detection/yolo11n.onnx",task="detect")
print("model loaded; trying .track() with botsort.yaml ...", flush=True)
rclpy.init(); n=G(); t0=time.time(); last=-1; nframes=0; ids_seen=set(); idframes=0; err=None
while rclpy.ok() and time.time()-t0<26:
    rclpy.spin_once(n,timeout_sec=0.2)
    if n.seq!=last and n.frame is not None:
        last=n.seq; nframes+=1
        try:
            r=m.track(n.frame, persist=True, tracker='botsort.yaml', classes=[0], conf=0.35, verbose=False)[0]
        except Exception as e:
            err=str(e); break
        ids=None
        if r.boxes is not None and r.boxes.id is not None:
            ids=[int(x) for x in r.boxes.id.tolist()]
            ids_seen.update(ids); idframes+=1
        nb=0 if r.boxes is None else len(r.boxes)
        print("frame %d: persons=%d ids=%s"%(nframes,nb,ids), flush=True)
if err: print("TRACK ERROR:", err, flush=True)
print("SUMMARY frames=%d idframes=%d unique_ids=%s"%(nframes,idframes,sorted(ids_seen)), flush=True)
rclpy.shutdown()

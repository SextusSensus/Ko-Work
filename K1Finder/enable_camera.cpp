#include <iostream>
#include <string>
#include <booster/robot/channel/channel_factory.hpp>
#include <booster/robot/x5_camera/x5_camera_client.hpp>

using namespace booster::robot;

int main(int argc, char **argv) {
    std::string iface = (argc > 1) ? argv[1] : "127.0.0.1";
    int mode = (argc > 2) ? std::stoi(argv[2]) : 2; // 2 = kCameraModeNormalEnable
    ChannelFactory::Instance()->Init(0, iface);
    x5_camera::X5CameraClient cam;
    cam.Init();
    int r = cam.ChangeMode(static_cast<x5_camera::CameraSetMode>(mode));
    std::cout << "ChangeMode(" << mode << ") -> " << r << std::endl;
    x5_camera::GetStatusResponse st;
    int r2 = cam.GetStatus(st);
    std::cout << "GetStatus -> " << r2 << " status=" << (int)st.status_ << std::endl;
    return r;
}

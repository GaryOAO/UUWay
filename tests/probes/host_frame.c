#include "native_host_frame.h"
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"FAIL %d: %s\n",__LINE__,#x); return 1; } } while (0)
int main(void)
{
    VkInstance instance;
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo create = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    CHECK(vkCreateInstance(&create, NULL, &instance) == VK_SUCCESS);
    VkPhysicalDevice devices[16], physical = VK_NULL_HANDLE; uint32_t n = 16;
    CHECK(vkEnumeratePhysicalDevices(instance, &n, devices) == VK_SUCCESS);
    for (unsigned i=0;i<n;i++) { VkPhysicalDeviceProperties p; vkGetPhysicalDeviceProperties(devices[i],&p); if(p.vendorID==0x1002)physical=devices[i]; }
    CHECK(physical);
    VkQueueFamilyProperties families[32]; n=32; vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,families);
    unsigned family=0; while(family<n && !(families[family].queueFlags&VK_QUEUE_GRAPHICS_BIT))++family; CHECK(family<n);
    float priority=1;
    VkDeviceQueueCreateInfo queue_info={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=family,.queueCount=1,.pQueuePriorities=&priority};
    VkDeviceCreateInfo dc={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&queue_info};
    VkDevice device; CHECK(vkCreateDevice(physical,&dc,NULL,&device)==VK_SUCCESS);
    VkQueue queue; vkGetDeviceQueue(device,family,0,&queue);
    VkImageCreateInfo ic={.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,.imageType=VK_IMAGE_TYPE_2D,.format=VK_FORMAT_B8G8R8A8_UNORM,
        .extent={640,360,1},.mipLevels=1,.arrayLayers=1,.samples=VK_SAMPLE_COUNT_1_BIT,.tiling=VK_IMAGE_TILING_OPTIMAL,
        .usage=VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT};
    VkImage image; CHECK(vkCreateImage(device,&ic,NULL,&image)==VK_SUCCESS);
    VkMemoryRequirements req; vkGetImageMemoryRequirements(device,image,&req);
    unsigned type=0;while(!(req.memoryTypeBits&(1u<<type)))++type;
    VkMemoryAllocateInfo ma={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.allocationSize=req.size,.memoryTypeIndex=type};
    VkDeviceMemory memory; CHECK(vkAllocateMemory(device,&ma,NULL,&memory)==VK_SUCCESS); CHECK(vkBindImageMemory(device,image,memory,0)==VK_SUCCESS);
    struct uurb_host_frame *host=uurb_host_frame_open(physical,device,640,360); CHECK(host);
    VkCommandPoolCreateInfo pc={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,.queueFamilyIndex=family};
    VkCommandPool pool;CHECK(vkCreateCommandPool(device,&pc,NULL,&pool)==VK_SUCCESS);
    VkCommandBufferAllocateInfo ca={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=pool,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
    VkCommandBuffer cmd; CHECK(vkAllocateCommandBuffers(device,&ca,&cmd)==VK_SUCCESS);
    for(unsigned frame=0;frame<4;frame++) {
        CHECK(vkResetCommandBuffer(cmd,0)==VK_SUCCESS);
        VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};CHECK(vkBeginCommandBuffer(cmd,&begin)==VK_SUCCESS);
        VkImageMemoryBarrier b={.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,.srcAccessMask=frame?VK_ACCESS_TRANSFER_WRITE_BIT:0,
            .dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT,.oldLayout=frame?VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED,
            .newLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,
            .image=image,.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
        vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,NULL,0,NULL,1,&b);
        VkClearColorValue color={.float32={frame&1?0:1,0,frame&1?1:0,1}};
        vkCmdClearColorImage(cmd,image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,&color,1,&b.subresourceRange);
        uurb_host_frame_record(host,cmd,image);CHECK(vkEndCommandBuffer(cmd)==VK_SUCCESS);
        VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cmd};
        CHECK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE)==VK_SUCCESS);CHECK(vkQueueWaitIdle(queue)==VK_SUCCESS);
        int fd=uurb_host_frame_publish(host);CHECK(fd>=0);
        const unsigned char *pixels=mmap(NULL,640*360*4,PROT_READ,MAP_SHARED,fd,0);CHECK(pixels!=MAP_FAILED);
        for(unsigned i=0;i<640*360;i++) CHECK(pixels[i*4]==(frame&1?255:0)&&pixels[i*4+1]==0&&pixels[i*4+2]==(frame&1?0:255)&&pixels[i*4+3]==255);
        CHECK(ftruncate(fd,1)!=0);munmap((void*)pixels,640*360*4);
    }
    uurb_host_frame_close(host);vkDestroyCommandPool(device,pool,NULL);vkDestroyImage(device,image,NULL);vkFreeMemory(device,memory,NULL);
    vkDestroyDevice(device,NULL);vkDestroyInstance(instance,NULL);
    puts("Vulkan readback: four changing frames, every BGRA pixel, sealed memfd verified");return 0;
}

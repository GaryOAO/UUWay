/* Metadata-only Vulkan import validation; does not read or execute GPU work. */
#ifndef UURB_VK_DMABUF_IMPORT_H
#define UURB_VK_DMABUF_IMPORT_H
#include <vulkan/vulkan.h>
#include <stdint.h>
struct uurb_vk_importer;
struct uurb_vk_image { VkImage image; VkDeviceMemory memory; };
struct uurb_vk_importer *uurb_vk_importer_open(VkPhysicalDevice physical);
struct uurb_vk_importer *uurb_vk_importer_open_transfer(VkPhysicalDevice physical);
void uurb_vk_importer_context(struct uurb_vk_importer *, VkDevice *, VkQueue *, uint32_t *);
int uurb_vk_import_image(struct uurb_vk_importer *, int fd, uint32_t width, uint32_t height,
                        uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride,
                        VkImageUsageFlags usage, struct uurb_vk_image *);
void uurb_vk_import_image_close(struct uurb_vk_importer *, struct uurb_vk_image *);
int uurb_vk_importer_check(struct uurb_vk_importer *, int fd, uint32_t width, uint32_t height,
                           uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride);
void uurb_vk_importer_close(struct uurb_vk_importer *);
#endif

import ray120c

# ray120c.set_cct(1800, intensity_percent=1, gm=200)  # 最偏 G
# ray120c.set_cct(20000, intensity_percent=1, gm=100)    # 最偏 M
# print(ray120c.get_cct())

# ray120c.set_rgb(0, 0, 0)  # 最暗
# print(ray120c.get_rgb())

ray120c.set_hsl(60, 50, intensity=10)  # 最暗

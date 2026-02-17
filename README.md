# Beacon Klipper (Hybrid Fast Homing Fork)

**Note:** This is a modified fork of the official [Beacon Klipper module](https://github.com/beacon3d/beacon_klipper).

### The Goal: Faster Homing without sacrificing accuracy
This fork implements a **Hybrid Proximity/Contact approach**:
1.  **Fast Approach (Proximity):** The toolhead uses the Beacon's proximity sensor to move quickly towards the bed until the proximity sensor triggers (~3mm depending on hardware).
2.  **Final homing (Contact):** It then switches to the standard, high-precision contact mode for the final few millimeters until the nozzle contacts the bed.

This allows you to set a higher travel speed for the majority of the movement while keeping the contact event slow and accurate.

### Requirements/Limitations
This breaks bed meshing. Also, to use this you MUST have already tested that proximity homing works on your machine. 

### Configuration
To enable this feature, simply add `approach_speed` to your `[beacon]` config section.

#### Implementation Example (`printer.cfg`)

```ini
[beacon]
# -- Existing beacon config... ---

# -- Required config for this ---
default_probe_method: contact 
home_method: contact 
home_method_when_homed: contact 
home_autocalibrate: never 

# --- Custom Fast Approach Settings ---
autocal_speed: 3.0      # This is the speed for the final approach until the nozzle contacts the bed.
                        # This is not required and defaults to 3 mm/s. 
                      
approach_speed: 3.0     # Speed for the initial approach. This defaults to 3 mm/s. 
                        # Test with this slow and then increase as you like. I run this at 15 mm/s on my Vz330.
```
					  
### Installation & Switching
If you already have Beacon installed, you can switch to this fork by updating your `moonraker.conf` and pulling the new code.

1.  **Edit `moonraker.conf`**
    Find your existing `[update_manager beacon]` section and change the `origin` to point to this fork.
    ```ini
    [update_manager beacon]
    type: git_repo
    path: ~/beacon_klipper
    origin: https://github.com/matt-rendall/beacon_klipper_hybrid_approach.git
    primary_branch: master
    install_script: install.sh
    ```

2.  **Switch the Repository**
    SSH into your Pi and run the following commands to switch the remote origin to this fork:
    ```bash
    cd ~/beacon_klipper
    git remote set-url origin https://github.com/matt-rendall/beacon_klipper_hybrid_approach.git
    git fetch origin
    git reset --hard origin/master
    ./install.sh
    ```

3.  **Restart**
    Restart Klipper and Moonraker. You should now see the version tracking this fork in Mainsail/Fluidd.